# Screen Translator

Windows-приложение: перевод выделенного текста с заменой + OCR области экрана (RU ↔ EN).

## Возможности

- **Shift + Alt + S** — перевести выделенный текст и заменить его
- **Shift + Alt + X** — выделить область экрана, распознать и перевести
- **Ctrl + Enter** — перевод текста в окне приложения
- История переводов, тёмная/светлая тема, настраиваемые горячие клавиши
- Свой API-ключ OCR.space в настройках

## Быстрый старт (из исходников)

1. Установите [Python 3.11+](https://www.python.org/downloads/) (галочка **Add to PATH**).
2. Откройте PowerShell в папке проекта:

```powershell
cd путь\к\ScreenTranslator
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

3. (Рекомендуется) Получите бесплатный ключ на https://ocr.space/ocrapi и вставьте в **Настройки → API ключ OCR**.

## Сборка EXE (GitHub Actions)

1. Залейте файлы в репозиторий (включая `.github/workflows/build.yml`).
2. **Actions → Build Windows EXE → Run workflow**.
3. Скачайте артефакт `ScreenTranslator-Windows`.

Либо локально:

```powershell
pip install -r requirements.txt
pyinstaller --clean --noconfirm --onefile --windowed --name ScreenTranslator app.py
```

EXE появится в `dist\ScreenTranslator.exe`.

## Важно

- Если целевая программа запущена **от администратора**, запускайте Screen Translator тоже от администратора.
- Бесплатный ключ OCR `helloworld` имеет жёсткие лимиты — лучше свой ключ.
- Интернет нужен для OCR и перевода.

## Горячие клавиши по умолчанию

| Комбинация        | Действие                          |
|-------------------|-----------------------------------|
| Shift + Alt + S   | Заменить выделенный текст         |
| Shift + Alt + X   | Перевод области экрана            |
| Ctrl + Enter      | Перевести текст в окне            |
