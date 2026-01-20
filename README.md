# Automacao de Contas (MVP) • Document Intelligence Vault

[![Django](https://img.shields.io/badge/Django-4%2B-092E20?logo=django&logoColor=white)](https://www.djangoproject.com/)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](https://docs.docker.com/compose/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-DB-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)

A **Django** MVP for **multi-file upload**, **OCR + text extraction**, **per-document (and batch) processing**, **JSON results storage**, **protected download**, and **search/filter by keywords**.

> ✅ Current focus: a **navigable document database** (e.g., HR uploads 50 resumes and filters by keywords).  
> 🔜 Next: stronger extraction rules, better classification, and production hardening.

---

## 🌐 Languages

- [Português (BR)](#-português-br)
- [English](#-english)

---

# 🇧🇷 Português (BR)

## Visão geral

Este sistema permite:

- Upload de **vários documentos** (PDF) de uma vez
- Cada arquivo vira **uma linha** na tabela de documentos
- **Processamento** extrai texto do PDF; se necessário, usa **OCR** (Tesseract)
- Resultado é salvo em um **JSON limpo** (apenas dados extraídos)
- Debug fica no **log**, com eventos estruturados por documento/campo
- Download dos arquivos é **protegido por login**
- Filtro/busca por **palavras-chave** na listagem

---

## Stack

- **Backend:** Django (Python 3.11+)
- **DB (recomendado):** PostgreSQL (via Docker)
- **OCR:** Tesseract + Poppler (pdftoppm) + `pdf2image`/`pytesseract`
- **Execução:**
  - ✅ Docker + Docker Compose (ambiente replicável)
  - Alternativo: venv + `python manage.py runserver`

---

## Requisitos

### Opção A — Recomendado (Docker)
- Docker
- Docker Compose

### Opção B — Local (sem Docker)
- Python 3.11+
- pip
- (Opcional) deps do OCR no sistema: `tesseract-ocr` + `poppler-utils`

---

## Começando rápido (Docker + Postgres + OCR)

### 1) Criar `.env`

Crie um arquivo `.env` na raiz do projeto:

```bash
DEBUG=1
SECRET_KEY=change-me
ALLOWED_HOSTS=127.0.0.1,localhost

# Postgres (docker compose)
DATABASE_URL=postgres://postgres:postgres@db:5432/postgres

# OCR (opcional)
OCR_LANG=por
```

> Se `ALLOWED_HOSTS` estiver bloqueando acesso na rede local, adicione o IP da máquina (ex: `192.168.0.10`) e/ou `0.0.0.0`.

---

### 2) Subir os containers

```bash
docker compose up -d --build
```

### 3) Criar superusuário

```bash
docker compose exec web python manage.py createsuperuser
```

### 4) Acessar

- Login: http://127.0.0.1:8000/login/
- Lista: http://127.0.0.1:8000/documents/
- Upload: http://127.0.0.1:8000/documents/upload/

---

## Desenvolvimento (como atualizar o container sem “recriar tudo”)

### Mudou apenas código Python/HTML/CSS?
Se o `docker-compose.yml` estiver montando volume do projeto no container (bind mount), normalmente **é instantâneo** (refresh no browser).

Se não estiver, ou se você preferir rebuild controlado:

```bash
docker compose up -d --build web
```

### Mudou dependências (`requirements.txt`) ou Dockerfile?
Precisa rebuild:

```bash
docker compose build web
docker compose up -d web
```

### Rodar comandos Django dentro do container

```bash
docker compose exec web python manage.py migrate
docker compose exec web python manage.py collectstatic --noinput
```

### Ver logs

```bash
docker compose logs -f web
```

---

## Rodar local (sem Docker)

> Útil para iterar muito rápido. Recomendado manter o Docker como “fonte da verdade” do ambiente.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

---

## OCR (detalhes)

O OCR é acionado quando o PDF não tem texto “selecionável”.

**Dependências Python:**
- `pdf2image`
- `pytesseract`

**Dependências de sistema (Linux):**
- `tesseract-ocr`
- `poppler-utils` (fornece `pdftoppm`)

**Variável opcional:**
- `OCR_LANG=por` (se o pacote do idioma estiver instalado no Tesseract)

---

## Como funciona (fluxo do usuário)

1. Login
2. Upload de PDFs
3. Processar documento (ou lote, se habilitado)
4. Visualizar JSON extraído
5. Filtrar/buscar por palavras-chave na listagem
6. Fazer download do documento por linha

---

## Estrutura do projeto

- `automacao_contas/` — settings/urls
- `documents/` — models/views/forms/services/extractors
- `templates/` — HTML
- `static/` — CSS/JS
- `staticfiles/` — saída do `collectstatic` (Docker/prod)
- `media/` — uploads

---

## Logs e Debug

- O JSON salvo deve ficar **limpo** (somente dados extraídos).
- Debug fica no **log** com eventos como:
  - `upload_documents`
  - `process_document_start`
  - `ocr_fallback`
  - `extract_ok` / `extract_missing`
  - `process_document_done`

---

## Fluxo comum com mais devs (GitHub/GitLab)

1) Atualize a `main` local

```bash
git switch main
git pull origin main
```

2) Crie uma branch de feature

```bash
git switch -c feature/minha-feature
```

3) Commit + push

```bash
git add .
git commit -m "feat: minha feature"
git push -u origin feature/minha-feature
```

4) Abra um **Merge Request / Pull Request** no GitLab/GitHub  
5) Review → Merge → apagar branch (opcional)

---

## Troubleshooting

### “dj_database_url não encontrado”
Garanta que está no `requirements.txt` e instalado.

- Local:
```bash
pip install dj-database-url
```

- Docker:
```bash
docker compose build --no-cache web
docker compose up -d web
```

### “PDF sem texto”
Documento provavelmente escaneado → precisa OCR. Veja se apareceu `ocr_fallback` no log.

### Migrações não aplicadas
```bash
docker compose exec web python manage.py migrate
```

---

# 🇺🇸 English

## Overview

This system provides:

- **Multi-file** PDF upload
- Each file becomes **one row** in the documents table
- **Processing** extracts PDF text; falls back to **OCR** (Tesseract) for scanned PDFs
- Results are stored as a **clean JSON** (only extracted fields)
- Debug/telemetry lives in **structured logs**
- File download is **login-protected**
- List page supports **keyword search/filter**

---

## Tech stack

- **Backend:** Django (Python 3.11+)
- **DB (recommended):** PostgreSQL (Docker)
- **OCR:** Tesseract + Poppler (pdftoppm) + `pdf2image`/`pytesseract`
- **Run modes:**
  - ✅ Docker + Docker Compose (replicable environment)
  - Alternative: venv + `python manage.py runserver`

---

## Requirements

### Option A — Recommended (Docker)
- Docker
- Docker Compose

### Option B — Local (no Docker)
- Python 3.11+
- pip
- (Optional) OCR deps: `tesseract-ocr` + `poppler-utils`

---

## Quickstart (Docker + Postgres + OCR)

### 1) Create a `.env`

Create a `.env` file at the project root:

```bash
DEBUG=1
SECRET_KEY=change-me
ALLOWED_HOSTS=127.0.0.1,localhost

# Postgres (docker compose)
DATABASE_URL=postgres://postgres:postgres@db:5432/postgres

# OCR (optional)
OCR_LANG=por
```

> For LAN access, add your machine IP (e.g., `192.168.0.10`) and/or `0.0.0.0` to `ALLOWED_HOSTS`.

### 2) Start the stack

```bash
docker compose up -d --build
```

### 3) Create a superuser

```bash
docker compose exec web python manage.py createsuperuser
```

### 4) Open

- Login: http://127.0.0.1:8000/login/
- Documents list: http://127.0.0.1:8000/documents/
- Upload: http://127.0.0.1:8000/documents/upload/

---

## Development workflow (updating containers)

### Only changed Python/HTML/CSS?
If your compose uses a bind mount (project folder mapped into the container), changes are usually **instant**.

If not, or for a controlled rebuild:

```bash
docker compose up -d --build web
```

### Changed dependencies (`requirements.txt`) or Dockerfile?
Rebuild:

```bash
docker compose build web
docker compose up -d web
```

### Run Django commands inside the container

```bash
docker compose exec web python manage.py migrate
docker compose exec web python manage.py collectstatic --noinput
```

### Follow logs

```bash
docker compose logs -f web
```

---

## Run locally (no Docker)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

---

## OCR notes

OCR is used when PDFs have no selectable text.

**Python deps:**
- `pdf2image`
- `pytesseract`

**System deps (Linux):**
- `tesseract-ocr`
- `poppler-utils` (`pdftoppm`)

**Optional:**
- `OCR_LANG=por` (if language pack is installed)

---

## Typical multi-dev Git workflow (GitHub/GitLab)

```bash
git switch main
git pull origin main
git switch -c feature/my-feature
# work...
git add .
git commit -m "feat: my feature"
git push -u origin feature/my-feature
```

Then open a Merge Request / Pull Request.

---

## License

Add one (MIT/Apache-2.0/etc.) if the repo is public.