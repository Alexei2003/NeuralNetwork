
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, Dataset
import numpy as np
import os
from glob import glob
from PIL import Image
import onnxruntime as ort
from sklearn.metrics import precision_score, recall_score, f1_score
import time
from torchinfo import summary
import albumentations as A
from albumentations.pytorch import ToTensorV2
import torch.backends.cudnn

# ====================== КОНФИГУРАЦИЯ ======================
class Config:
    # Пути к данным и моделям
    source_dir = "/media/alex/Programs/NeuralNetwork/DataSet/ARTS/Original"         # Папка с исходными изображениями
    checkpoint_path = "/media/alex/Programs/NeuralNetwork/Model/best_model.pth"     # Путь для сохранения/загрузки модели
    labels_path = "/media/alex/Programs/NeuralNetwork/Model/labels.txt"             # Файл с метками классов
    onnx_path = "/media/alex/Programs/NeuralNetwork/Model/model.onnx"               # Путь для экспортированной модели в ONNX формате

    # Флаги управления обучением
    resume_training = False         # Продолжать обучение с сохраненного чекпоинта, если True

    # Параметры входных данных
    input_size = (224, 224)         # Размер входного изображения (ширина, высота)

    # Архитектура модели и гиперпараметры
    num_experts = 8                 # Количество экспертов в MoE (Mixture of Experts)
    expert_units = 1024             # Количество нейронов в каждом эксперте
    k_top_expert = 2                # Количество активных экспертов на один пример
    se_reduction = 16               # Коэффициент редукции для SE (Squeeze-and-Excitation) блока
    dropout = 0.5                   # Вероятность отключения нейронов (dropout)

    # Параметры обучения
    accumulation_steps = 5          # Количество шагов накопления градиентов (для эффективного увеличения размера батча без увеличения памяти)
    lr = 0.004 * accumulation_steps # Начальная скорость обучения (learning rate), масштабируется под accumulation_steps для стабильности
    batch_size = 100                # Размер батча (число примеров, обрабатываемых за один проход)
    epochs = 100                    # Количество эпох обучения (полных проходов по всему датасету)
    focal_gamma = 5                 # Параметр гамма для Focal Loss, регулирует степень фокусировки на сложных примерах
    smoothing = 0.1                 # Параметр label smoothing, задаёт уровень сглаживания меток для улучшения обобщения
    threshold = 1e-2                # Разница val loss для уменьшения learning rate

    # Настройки оптимизации и контроля обучения
    mixed_precision = True          # Использовать смешанную точность (fp16) для ускорения обучения
    early_stopping_patience = 5     # Количество эпох без улучшения для ранней остановки
    val_split = 0.2                 # Доля данных, выделяемая под валидацию
    factor_lr = 0.5                 # Коэффициент уменьшения learning rate при plateau
    patience_lr = 1                 # Количество эпох без улучшения для снижения learning rate

config = Config()

# ====================== КОМПОНЕНТЫ МОДЕЛИ ======================
class MoE(nn.Module):
    def __init__(self, input_dim, num_experts, expert_units, k_top):
        super().__init__()
        self.num_experts = num_experts
        self.k_top = k_top
        self.experts = nn.ModuleList([
            nn.Sequential(
                nn.Linear(input_dim, expert_units),
                nn.ReLU(inplace=True) ,
                nn.Dropout(config.dropout),
                nn.Linear(expert_units, input_dim))
            for _ in range(num_experts)
        ])
        self.router = nn.Linear(input_dim, num_experts)

    def forward(self, x):
        logits = self.router(x)
        top_k_weights, top_k_indices = logits.topk(self.k_top, dim=1)
        top_k_weights = torch.softmax(top_k_weights, dim=1)
        
        # Создаем тензор всех экспертных выходов
        expert_outputs = torch.stack([expert(x) for expert in self.experts], dim=1)  # [B, num_experts, D]
        
        # Создаем маску для выбранных экспертов [B, num_experts, D]
        mask = torch.zeros_like(expert_outputs)
        mask = torch.scatter(
            mask, 
            1, 
            top_k_indices.unsqueeze(-1).expand(-1, -1, expert_outputs.size(-1)),
            1.0
        )
        
        # Объединяем градиенты только для выбранных экспертов
        expert_outputs = expert_outputs * mask + (expert_outputs * (1 - mask)).detach()
        
        # Выбираем топ-k экспертов
        selected_outputs = expert_outputs.gather(
            1, 
            top_k_indices.unsqueeze(-1).expand(-1, -1, expert_outputs.size(-1))
        )
        
        # Взвешенное суммирование
        output = (selected_outputs * top_k_weights.unsqueeze(-1)).sum(dim=1)
        return output + x

class SEBlock(nn.Module):
    def __init__(self, channels):
        super().__init__()
        reduced = max(1, channels // config.se_reduction)
        self.se = nn.Sequential(
            nn.AdaptiveAvgPool2d(1),
            nn.Conv2d(channels, reduced, 1),
            nn.ReLU(inplace=True) ,
            nn.Conv2d(reduced, channels, 1),
            nn.Sigmoid()
        )

    def forward(self, x):
        return x * self.se(x)

class ResidualBlock(nn.Module):
    def __init__(self, in_channels, out_channels, stride=1):
        super().__init__()
        self.conv1 = nn.Conv2d(in_channels, out_channels, 3, stride, padding=1, bias=False)
        self.bn1 = nn.BatchNorm2d(out_channels)
        self.conv2 = nn.Conv2d(out_channels, out_channels, 3, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(out_channels)
        self.se = SEBlock(out_channels)
        self.act = nn.ReLU(inplace=True) 
        self.dropout = nn.Dropout2d(config.dropout)  # Добавлен Dropout

        self.shortcut = nn.Sequential()
        if stride != 1 or in_channels != out_channels:
            self.shortcut = nn.Sequential(
                nn.Conv2d(in_channels, out_channels, 1, stride, bias=False),
                nn.BatchNorm2d(out_channels)
            )

    def forward(self, x):      
        residual = self.shortcut(x)
        x = self.act(self.bn1(self.conv1(x)))
        x = self.dropout(x)  # Добавлен Dropout
        x = self.bn2(self.conv2(x))
        x = self.se(x)
        return self.act(x + residual)

class AnimeClassifier(nn.Module):
    def __init__(self, num_classes):
        super().__init__()
        self.backbone = nn.Sequential(
            nn.Conv2d(3, 64, 7, stride=2, padding=3, bias=False),
            nn.BatchNorm2d(64),
            nn.ReLU(inplace=True) ,
            nn.MaxPool2d(3, stride=2, padding=1),
            ResidualBlock(64, 64),
            ResidualBlock(64, 128, stride=2),
            ResidualBlock(128, 256, stride=2),
            ResidualBlock(256, 512, stride=2),
            nn.AdaptiveAvgPool2d(1)
        )
        self.moe = MoE(512, config.num_experts, config.expert_units, config.k_top_expert)
        self.head = nn.Sequential(
            nn.Dropout(config.dropout),
            nn.Linear(512, num_classes)

        )

    def forward(self, x):
        x = self.backbone(x).flatten(1)
        x = self.moe(x)
        return self.head(x)

# ====================== ОБРАБОТКА ДАННЫХ ======================
class ImageDataset(Dataset):
    def __init__(self, root, transform=None, mode='train'):
        if os.path.exists(config.labels_path):
            with open(config.labels_path, 'r') as f:
                self.classes = [line.strip() for line in f]
        else:
            self.classes = sorted(os.listdir(root))
        
        self.samples = []
        for label, cls in enumerate(self.classes):
            cls_path = os.path.join(root, cls)
            self.samples.extend([(f, label) for f in glob(os.path.join(cls_path, '*'))])
        
        self.transform = transform or self._get_transforms(mode)

    def __len__(self): 
        return len(self.samples)

    def __getitem__(self, idx):
        img_path, label = self.samples[idx]
        img = np.array(Image.open(img_path).convert('RGB'))  # Конвертация в numpy array
        
        if self.transform:
            augmented = self.transform(image=img)
            img = augmented['image']
            
        return img, label

    @staticmethod
    def _get_transforms(mode):
        if mode == 'train':
            return A.Compose([
                A.Rotate(limit=30, p=0.5),
                A.RandomResizedCrop(
                    size=config.input_size,
                    scale=(0.8, 1.0),
                    ratio=(0.75, 1.33),          # Опционально (по умолчанию (0.75, 1.33))
                    interpolation=1,             # BILINEAR
                    p=1.0
                ),
                A.HorizontalFlip(p=0.5),
                A.ColorJitter(
                    brightness=0.2,
                    contrast=0.2,
                    saturation=0.2,
                    hue=0.0,                     # Обязательный параметр
                    p=0.5
                ),
                A.GaussianBlur(blur_limit=(3, 3), p=0.2),
                A.Affine(
                    translate_percent=(-0.1, 0.1),
                    keep_ratio=True,
                    p=0.5
                ),
                A.ToFloat(max_value=255.0),
                ToTensorV2(),
            ])
        return A.Compose([
            A.ToFloat(max_value=255.0),
            ToTensorV2(),
        ])

# ====================== ОБУЧЕНИЕ ======================
def focal_loss_with_smoothing(outputs, targets, gamma=5.0, smoothing=0.1):
    num_classes = outputs.size(1)
    confidence = 1.0 - smoothing

    log_probs = torch.nn.functional.log_softmax(outputs, dim=-1)
    probs = torch.exp(log_probs)

    # Сглаженные one-hot метки
    true_dist = torch.full_like(log_probs, smoothing / (num_classes - 1))
    true_dist.scatter_(1, targets.unsqueeze(1), confidence)

    pt = torch.sum(true_dist * probs, dim=-1)
    focal_factor = (1 - pt).pow(gamma)

    loss = -torch.sum(true_dist * log_probs, dim=-1)
    return torch.mean(focal_factor * loss)

def compile_model(model):
    torch.compile(model, 
        mode="max-autotune", 
        dynamic=False, 
        fullgraph=False)
    torch.cuda.empty_cache()  

def run_training():
    # Оптимизация матричных операций (НОВОЕ)
    torch.set_float32_matmul_precision('medium')

    # Включение оптимизации cuDNN
    torch.backends.cudnn.benchmark = True

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    os.makedirs(os.path.dirname(config.checkpoint_path), exist_ok=True)

    if config.resume_training and os.path.exists(config.labels_path):
        # Загружаем старые классы и добавляем новые в конец
        with open(config.labels_path, 'r') as f:
            old_classes = [line.strip() for line in f]
        current_classes = sorted(os.listdir(config.source_dir))
        new_classes = [cls for cls in current_classes if cls not in old_classes]
        full_classes = old_classes + new_classes
        if new_classes:
            print(f"Добавлены новые классы в файл: {', '.join(new_classes)}")
        else:
            print("Новых классов не найдено.")
    else:
        full_classes = sorted(os.listdir(config.source_dir))

    # Сохраняем обновленный список классов
    with open(config.labels_path, 'w') as f:
        f.write('\n'.join(full_classes))

    # Создаем датасет с фиксированным порядком классов
    full_dataset = ImageDataset(config.source_dir)
    full_dataset.classes = full_classes  # Переопределяем порядок

    train_size = int((1 - config.val_split) * len(full_dataset))
    train_ds, val_ds = torch.utils.data.random_split(full_dataset, [train_size, len(full_dataset) - train_size])

    train_loader = DataLoader(
        train_ds, 
        batch_size=config.batch_size, 
        shuffle=True, 
        num_workers=os.cpu_count(), 
        persistent_workers=True,            
        prefetch_factor=2, 
        pin_memory=True)
    val_loader = DataLoader(
        val_ds, 
        batch_size=config.batch_size, 
        num_workers=os.cpu_count(), 
        persistent_workers=True, 
        prefetch_factor=2, 
        pin_memory=True)

    model = AnimeClassifier(len(full_classes)).to(device)

    optimizer = optim.AdamW(model.parameters(), lr=config.lr)
    plateau_scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer,
        mode='min',
        factor=config.factor_lr,
        patience=config.patience_lr,
        threshold=config.threshold, # ← игнорирует минимальные изменения
        threshold_mode='rel',       # ← относительное сравнение
        verbose=True                # ← печатает сообщение при снижении LR
    )
    scaler = torch.amp.GradScaler('cuda', enabled=config.mixed_precision and torch.cuda.is_available())
    start_epoch = 0
    best_loss = float('inf')
    early_stop_counter = 0

    if config.resume_training:
        checkpoint = torch.load(config.checkpoint_path)
        
        # Удаление префикса _orig_mod. из ключей (если есть)
        checkpoint['model_state_dict'] = {
            k.replace("_orig_mod.", ""): v 
            for k, v in checkpoint['model_state_dict'].items()
        }
        
        # Поиск ключа для весов head слоя
        head_weight_key = next(
            (k for k in checkpoint['model_state_dict'] 
            if 'head' in k and 'weight' in k and k.endswith('.weight')), 
            None
        )
        if not head_weight_key:
            raise KeyError("❌ Ключ для весов head слоя не найден в чекпоинте!")
        
        saved_num_classes = checkpoint['model_state_dict'][head_weight_key].shape[0]
        current_num_classes = len(full_dataset.classes)

        # Частичная загрузка весов (игнорируем head слой)
        model.load_state_dict(
            {k: v for k, v in checkpoint['model_state_dict'].items() 
            if not ('head' in k and ('weight' in k or 'bias' in k))},
            strict=False
        )

        # Инициализация новых весов
        if current_num_classes > saved_num_classes:
            print(f"🆕 Добавлено {current_num_classes - saved_num_classes} новых классов")
            
            # Инициализация новых весов
            nn.init.kaiming_normal_(
                model.head[1].weight.data[saved_num_classes:],
                mode='fan_out',
                nonlinearity='linear'
            )
            nn.init.constant_(
                model.head[1].bias.data[saved_num_classes:], 
                0.0
            )

            # Пересоздаем оптимизатор с новыми параметрами
            optimizer = optim.AdamW(model.parameters(), lr=config.lr)
            
            # Загружаем состояние старого оптимизатора
            old_optimizer_state = checkpoint['optimizer_state_dict']
            
            # Создаем совместимое состояние
            new_optimizer_state = {
                'state': {},  # Инициализируем состояния с нуля
                'param_groups': old_optimizer_state['param_groups']  # Сохраняем настройки групп
            }
            
            # Загружаем состояние для совместимых параметров
            for param_name, param_state in old_optimizer_state['state'].items():
                if param_name in optimizer.state_dict()['state']:
                    new_optimizer_state['state'][param_name] = param_state
            
            optimizer.load_state_dict(new_optimizer_state)

            # Компиляция модели
            compile_model(model)

            # Восстановление остальных состояний
            start_epoch = checkpoint['epoch'] + 1
            best_loss = checkpoint['best_loss']
            early_stop_counter = checkpoint['early_stop_counter']

            print(f"🔄 Старт дообучения, новых классов: {current_num_classes - saved_num_classes}")     
        else:
            # Оптимизация модели
            compile_model(model)

            #4. Восстановление состояний
            optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
            plateau_scheduler.load_state_dict(checkpoint['scheduler_plateau'])
            scaler.load_state_dict(checkpoint['scaler_state_dict'])
            start_epoch = checkpoint['epoch'] + 1
            best_loss = checkpoint['best_loss']
            early_stop_counter = checkpoint['early_stop_counter']
            print(f"🔄 Продолжение обучения с эпохи {start_epoch}")
    else:
        # Оптимизация модели
        compile_model(model)

        torch.save({
            'epoch': -1,
            'model_state_dict': model.state_dict(),
            'optimizer_state_dict': optimizer.state_dict(),
            'scheduler_plateau': plateau_scheduler.state_dict(), 
            'scaler_state_dict': scaler.state_dict(),
            'best_loss': best_loss,
            'early_stop_counter': early_stop_counter,
        }, config.checkpoint_path)

    summary(model, input_size=(1, 3, 224, 224))

    start_time = time.time()  # Засекаем время начала

    optimizer.zero_grad(set_to_none=True)  # Инициализация градиентов
    for epoch in range(start_epoch, config.epochs):
        model.train()
        train_loss = 0.0
        train_correct, train_total = 0, 0
        optimizer.zero_grad() 
        accumulated_loss = 0.0  # Для агрегации потерь при накоплении

        epoch_start_time = time.time()  # Время начала эпохи
        for batch_idx, (inputs, labels) in enumerate(train_loader):
            batch_start_time = time.time()  # Время начала обработки батча
            inputs, labels = inputs.to(device), labels.to(device)
            
            with torch.amp.autocast('cuda', enabled=config.mixed_precision):
                outputs = model(inputs)
                loss = focal_loss_with_smoothing(outputs, labels, config.focal_gamma, config.smoothing) / config.accumulation_steps
            
            # Накопление градиентов (основное изменение)
            scaler.scale(loss).backward()
            accumulated_loss += loss.item() * config.accumulation_steps
            
            # Обновление только на последнем шаге накопления
            if (batch_idx + 1) % config.accumulation_steps == 0 or (batch_idx + 1) == len(train_loader):
                # Градиентный клиппинг
                scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
                
                # Шаг оптимизатора
                scaler.step(optimizer)
                scaler.update()
                optimizer.zero_grad(set_to_none=True)
                
                # Расчет метрик только при обновлении
                _, predicted = torch.max(outputs, 1)
                current_batch_size = labels.size(0)
                train_total += current_batch_size
                train_correct += (predicted == labels).sum().item()
                
                # Логирование только при обновлении
                batch_loss_value = accumulated_loss
                train_loss += batch_loss_value
                
                # Сброс накопленных потерь
                accumulated_loss = 0.0
                
                # Расчет времени для логирования
                batch_end_time = time.time()
                batch_duration = batch_end_time - batch_start_time
                remaining_batches = len(train_loader) - (batch_idx + 1)
                estimated_remaining_time = remaining_batches * batch_duration
                remaining_time_str = time.strftime('%H:%M:%S', time.gmtime(estimated_remaining_time))

                print(
                    f"\r[Train] Epoch {epoch+1}/{config.epochs} | Batch {batch_idx+1}/{len(train_loader)} | "
                    f"Loss: {batch_loss_value:.4f} | Remaining time: {remaining_time_str}",
                    end='', flush=True)
            else:
                # Пропускаем логирование на промежуточных шагах
                continue

        epoch_end_time = time.time()  # Время окончания эпохи
        epoch_duration = epoch_end_time - epoch_start_time
        total_elapsed_time = epoch_end_time - start_time
        epoch_duration_str = time.strftime("%H:%M:%S", time.gmtime(epoch_duration))
        total_elapsed_str = time.strftime("%H:%M:%S", time.gmtime(total_elapsed_time))

        train_accuracy = 100 * train_correct / train_total

        # Валидация
        model.eval()
        val_loss, val_correct, val_total = 0.0, 0, 0
        all_preds, all_labels = [], []
        
        val_loader_len = len(val_loader)

        with torch.inference_mode(), torch.amp.autocast('cuda', enabled=config.mixed_precision):
            for batch_idx, (inputs, labels) in enumerate(val_loader):
                batch_start_time = time.time()
                
                inputs, labels = inputs.to(device), labels.to(device)
                outputs = model(inputs)
                
                loss = focal_loss_with_smoothing(outputs, labels, config.focal_gamma, config.smoothing) 
                val_loss += loss.item()
                
                _, predicted = torch.max(outputs, 1)
                val_total += labels.size(0)
                val_correct += (predicted == labels).sum().item()

                all_preds.extend(predicted.cpu().numpy())
                all_labels.extend(labels.cpu().numpy())

                # Расчет оставшегося времени
                batch_end_time = time.time()
                batch_duration = batch_end_time - batch_start_time
                remaining_batches = val_loader_len - (batch_idx + 1)
                estimated_remaining_time = remaining_batches * batch_duration
                remaining_time_str = time.strftime('%H:%M:%S', time.gmtime(estimated_remaining_time))

                print(
                    f"\r[Val]   Epoch {epoch+1}/{config.epochs} | Batch {batch_idx+1}/{val_loader_len} | "
                    f"Loss: {loss.item():.4f} | Remaining: {remaining_time_str}",
                    end='', flush=True)
        
        print() 

        # Уменьшение скорости обучения      
        plateau_scheduler.step(val_loss)

        # Расчет метрик
        val_accuracy = 100 * val_correct / val_total
        val_precision = precision_score(all_labels, all_preds, average='macro', zero_division=0)
        val_recall = recall_score(all_labels, all_preds, average='macro', zero_division=0)
        val_f1 = f1_score(all_labels, all_preds, average='macro', zero_division=0)

        # Логирование
        print(f"[Summary] Train Loss: {train_loss/len(train_loader):.4f} | Acc: {train_accuracy:.2f}%")
        print(f"[Summary] Val   Loss: {val_loss/len(val_loader):.4f} | Acc: {val_accuracy:.2f}%")
        print(f"[Summary] Val Precision: {val_precision:.4f} | Recall: {val_recall:.4f} | F1: {val_f1:.4f}")
        print(f"[Time] Epoch: {epoch_duration_str} | Total: {total_elapsed_str}")
        current_lr = optimizer.param_groups[0]['lr']
        print(f"[Summary] LR: {current_lr:.10f}")
        print()

        # Ранняя остановка
        if val_loss < best_loss:
            best_loss = val_loss
            early_stop_counter = 0
            torch.save({
                'epoch': epoch,
                'model_state_dict': model.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'scheduler_plateau': plateau_scheduler.state_dict(), 
                'scaler_state_dict': scaler.state_dict(),
                'best_loss': best_loss,
                'early_stop_counter': early_stop_counter,
            }, config.checkpoint_path)
        else:
            early_stop_counter += 1
            if early_stop_counter >= config.early_stopping_patience:
                print("Ранняя остановка!")
                break

# ====================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ======================
def convert_to_onnx():
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    model = AnimeClassifier(len(get_classes())).to(device)
    checkpoint = torch.load(config.checkpoint_path)
    
    # Извлекаем веса модели из чекпоинта
    model_state_dict = checkpoint['model_state_dict']
    
    # Удаляем префикс _orig_mod. из ключей (если есть)
    model_state_dict = {
        k.replace("_orig_mod.", ""): v 
        for k, v in model_state_dict.items()
    }
    
    # Загружаем веса
    model.load_state_dict(model_state_dict)
    model.eval()
    
    # Экспорт в ONNX
    dummy_input = torch.randn(1, 3, *config.input_size).to(device)
    torch.onnx.export(
        model, 
        dummy_input, 
        config.onnx_path,
        input_names=['input'],
        output_names=['output'],
        dynamic_axes={'input': {0: 'batch_size'}, 'output': {0: 'batch_size'}},
        opset_version=13,
        do_constant_folding=True,
        training=torch.onnx.TrainingMode.EVAL,
        operator_export_type=torch.onnx.OperatorExportTypes.ONNX_ATEN_FALLBACK
    )
    print("✅ ONNX модель сохранена:", config.onnx_path)

def test_onnx():
    if not os.path.exists(config.onnx_path):
        print("ONNX модель не найдена!")
        return
    
    # Загрузка ONNX-модели
    session = ort.InferenceSession(config.onnx_path)
    
    try:
        img = Image.open("test.jpg").convert('RGB')
        img_np = np.array(img)

        # Применение преобразований
        transform = ImageDataset._get_transforms('val')
        augmented = transform(image=img_np)
        img_tensor = augmented['image'].unsqueeze(0)  
    except FileNotFoundError:
        print("Файл test.jpg не найден!")
        return

    # ====================== PyTorch предсказание ======================
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    
    # Инициализация и загрузка PyTorch-модели
    model = AnimeClassifier(len(get_classes())).to(device)
    checkpoint = torch.load(config.checkpoint_path)
    
    # Удаление префиксов _orig_mod. (если есть)
    model_state_dict = {
        k.replace("_orig_mod.", ""): v 
        for k, v in checkpoint['model_state_dict'].items()
    }
    model.load_state_dict(model_state_dict)
    model.eval()
    
    # Предсказание PyTorch
    with torch.no_grad():
        pytorch_output = model(img_tensor.to(device))
        pytorch_probs = torch.softmax(pytorch_output, dim=1).cpu()

    # ====================== ONNX предсказание ======================
    onnx_outputs = session.run(None, {'input': img_tensor.numpy().astype(np.float32)})
    onnx_probs = torch.softmax(torch.tensor(onnx_outputs[0]), dim=1)

    # ====================== Вывод результатов ======================
    with open(config.labels_path) as f:
        classes = [line.strip() for line in f]

    # Результаты PyTorch
    print("\n[PyTorch] Топ-5 предсказаний:")
    pytorch_top_probs, pytorch_top_indices = torch.topk(pytorch_probs, 5)
    for i, (prob, idx) in enumerate(zip(pytorch_top_probs[0], pytorch_top_indices[0])):
        print(f"{i+1}. {classes[idx]}: {prob.item()*100:.2f}%")

    # Результаты ONNX
    print("\n[ONNX] Топ-5 предсказаний:")
    onnx_top_probs, onnx_top_indices = torch.topk(onnx_probs, 5)
    for i, (prob, idx) in enumerate(zip(onnx_top_probs[0], onnx_top_indices[0])):
        print(f"{i+1}. {classes[idx]}: {prob.item()*100:.2f}%")

    # Проверка совпадения результатов
    diff = torch.max(torch.abs(pytorch_probs - onnx_probs)).item()
    print(f"\nРасхождение между выходами: {diff:.6f}")
    if diff > 0.001:
        print("⚠️ Возможная ошибка конвертации! Расхождение > 0.001")

def get_classes():
    with open(config.labels_path) as f:
        return [line.strip() for line in f]

# ====================== ИНТЕРФЕЙС ======================
def main_menu():
    while True:
        print("\nМеню:")
        print("1. Обучить модель")
        print("2. Продолжить обучение")  # Новая опция
        print("3. Конвертировать в ONNX")
        print("4. Протестировать ONNX")
        print("0. Выход")
        choice = input("Выбор: ").strip()
        
        if choice == '1':
            config.resume_training = False
            run_training()
        elif choice == '2':
            if not os.path.exists(config.checkpoint_path):
                print("❌ Чекпоинт для продолжения не найден!")
                continue
            config.resume_training = True
            run_training()
        elif choice == '3':
            convert_to_onnx()
        elif choice == '4':
            test_onnx()
        elif choice == '0':
            break
        else:
            print("Неверный ввод!")

if __name__ == "__main__":
    main_menu()
