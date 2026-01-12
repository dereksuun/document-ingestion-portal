# Automacao de Contas (MVP)

Simple Django MVP for multi-file upload, per-document processing, JSON storage, and protected download.

## Requirements

- Python 3.11+
- pip

## Get the project (GitHub download or clone)

Option A - Download ZIP:
1) Open the GitHub repo page.
2) Click "Code" -> "Download ZIP".
3) Unzip it and open a terminal in the project folder.

Option B - Clone:
```bash
git clone <REPO_URL> automacao_contas
cd automacao_contas
```

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py makemigrations
python manage.py migrate
python manage.py createsuperuser
```

## Run

```bash
python manage.py runserver
```

Open:
- Login: http://127.0.0.1:8000/login/
- Documents list: http://127.0.0.1:8000/documents/
- Upload: http://127.0.0.1:8000/documents/upload/

## How it works

- Upload multiple PDF files at once.
- Each file becomes a Document row in SQLite.
- Click "Processar" per row to extract text and build a JSON payload.
- The JSON is stored in `extracted_json`.
- Download is protected by login.

## Storage and database

- SQLite database: `db.sqlite3`
- Uploaded files: `media/`

## Notes and limitations (V1)

- OCR fallback is available for scanned PDFs (requires extra deps below).
- If OCR deps are missing or OCR yields no text, it fails with a clear message.
- Regex extraction is best-effort for: due date, amount, barcode/line.

## OCR dependencies (optional)

- Python: `pdf2image`, `pytesseract`
- System (Linux): `tesseract-ocr`, `poppler-utils` (for `pdftoppm`)
- Optional: set `OCR_LANG=por` if the Portuguese language pack is installed.

## Project structure

- `automacao_contas/` - Django project settings and URLs
- `documents/` - app with models, views, forms, services
- `templates/` - HTML templates
- `static/` - CSS

## Troubleshooting

- If the upload does nothing, check the server console for form errors.
- If you see "PDF sem texto", install OCR deps and retry.

=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-

MVP simples em Django para upload multiplo, processamento por documento, armazenamento de JSON e download protegido.

## Requisitos

- Python 3.11+
- pip

## Baixar o projeto (download do GitHub ou clone)

Opcao A - Download ZIP:
1) Abra a pagina do repositorio no GitHub.
2) Clique em "Code" -> "Download ZIP".
3) Descompacte e abra o terminal na pasta do projeto.

Opcao B - Clone:
```bash
git clone <REPO_URL> automacao_contas
cd automacao_contas
```

## Instalacao

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py makemigrations
python manage.py migrate
python manage.py createsuperuser
```

## Executar

```bash
python manage.py runserver
```

Acesse:
- Login: http://127.0.0.1:8000/login/
- Lista de documentos: http://127.0.0.1:8000/documents/
- Upload: http://127.0.0.1:8000/documents/upload/

## Como funciona

- Envie varios PDFs de uma vez.
- Cada arquivo vira uma linha Document no SQLite.
- Clique em "Processar" por linha para extrair texto e montar o JSON.
- O JSON fica salvo em `extracted_json`.
- Download protegido por login.
- Botao de tema alterna entre claro e escuro.

## Banco e armazenamento

- Banco SQLite: `db.sqlite3`
- Arquivos enviados: `media/`

## Notas e limitacoes (V1)

- OCR fica disponivel para PDFs escaneados (requer deps extras abaixo).
- Se faltar deps ou o OCR nao extrair texto, falha com uma mensagem clara.
- Extracao via regex e best-effort para: vencimento, valor, codigo de barras/linha.

## Dependencias de OCR (opcional)

- Python: `pdf2image`, `pytesseract`
- Sistema (Linux): `tesseract-ocr`, `poppler-utils` (para `pdftoppm`)
- Opcional: defina `OCR_LANG=por` se o pacote de idioma estiver instalado.

## Estrutura do projeto

- `automacao_contas/` - settings e URLs do projeto
- `documents/` - app com models, views, forms, services
- `templates/` - templates HTML
- `static/` - CSS

## Solucao de problemas

- Se o upload nao fizer nada, verifique o console do servidor por erros do formulario.
- Se aparecer "PDF sem texto", instale as dependencias de OCR e tente novamente.
