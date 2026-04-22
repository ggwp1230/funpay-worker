"""
FunPay Bot — Update Server
Запускается на VPS. Хранит актуальную версию и раздаёт обновления клиентам.

Установка на VPS:
  pip install fastapi uvicorn python-multipart
  python server.py

Загрузка обновления (из admin-панели или curl):
  curl -X POST http://your-vps:9000/admin/upload \
    -H "X-Admin-Token: YOUR_ADMIN_TOKEN" \
    -F "file=@update.zip"
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

# ── Config ────────────────────────────────────────────────────────────────────
ADMIN_TOKEN   = os.environ.get("ADMIN_TOKEN", "change_me_secret_admin_token")
ACCESS_TOKEN  = os.environ.get("ACCESS_TOKEN", "change_me_user_token")
PORT          = int(os.environ.get("PORT", 9000))

UPDATES_DIR   = Path("updates")
UPDATES_DIR.mkdir(exist_ok=True)

META_FILE     = UPDATES_DIR / "meta.json"
CURRENT_ZIP   = UPDATES_DIR / "current.zip"

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_meta() -> dict:
    if META_FILE.exists():
        try:
            return json.loads(META_FILE.read_text())
        except Exception:
            pass
    return {"version": "0.0.0", "changelog": "", "uploaded_at": 0, "size": 0, "sha256": ""}


def save_meta(meta: dict):
    META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def require_admin(token: Optional[str]):
    if token != ADMIN_TOKEN:
        raise HTTPException(403, "Invalid admin token")


def require_access(token: Optional[str]):
    if token != ACCESS_TOKEN:
        raise HTTPException(403, "Invalid access token")


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="FunPay Bot Update Server")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Client endpoints (защищены ACCESS_TOKEN) ──────────────────────────────────

@app.get("/version")
def get_version(x_token: Optional[str] = Header(None)):
    require_access(x_token)
    return load_meta()


@app.get("/download")
def download_update(x_token: Optional[str] = Header(None)):
    require_access(x_token)
    if not CURRENT_ZIP.exists():
        raise HTTPException(404, "No update available")
    return FileResponse(
        CURRENT_ZIP,
        media_type="application/zip",
        filename="update.zip"
    )


@app.get("/ping")
def ping():
    """Проверка доступности сервера (без токена)."""
    return {"ok": True, "time": time.time()}


# ── Admin endpoints (защищены ADMIN_TOKEN) ────────────────────────────────────

@app.post("/admin/upload")
async def upload_update(
    file: UploadFile = File(...),
    version: str = "1.0.0",
    changelog: str = "",
    x_admin_token: Optional[str] = Header(None)
):
    require_admin(x_admin_token)

    if not file.filename.endswith(".zip"):
        raise HTTPException(400, "Only .zip files accepted")

    # Сохраняем новый архив
    tmp = UPDATES_DIR / "upload_tmp.zip"
    with open(tmp, "wb") as f:
        shutil.copyfileobj(file.file, f)

    checksum = sha256_file(tmp)
    size = tmp.stat().st_size
    tmp.rename(CURRENT_ZIP)

    meta = {
        "version": version,
        "changelog": changelog,
        "uploaded_at": time.time(),
        "size": size,
        "sha256": checksum,
    }
    save_meta(meta)

    return {"ok": True, "version": version, "size": size, "sha256": checksum}


@app.get("/admin/status")
def admin_status(x_admin_token: Optional[str] = Header(None)):
    require_admin(x_admin_token)
    meta = load_meta()
    return {
        "current_version": meta["version"],
        "has_file": CURRENT_ZIP.exists(),
        "meta": meta,
    }


@app.delete("/admin/delete")
def delete_update(x_admin_token: Optional[str] = Header(None)):
    require_admin(x_admin_token)
    if CURRENT_ZIP.exists():
        CURRENT_ZIP.unlink()
    save_meta({"version": "0.0.0", "changelog": "", "uploaded_at": 0, "size": 0, "sha256": ""})
    return {"ok": True}


# ── Admin Web Panel ───────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
def admin_panel(x_admin_token: Optional[str] = Header(None, alias="x-admin-token")):
    """Веб-панель администратора."""
    meta = load_meta()
    size_kb = round(meta.get("size", 0) / 1024, 1)
    uploaded = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(meta.get("uploaded_at", 0))) \
               if meta.get("uploaded_at") else "—"

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FunPay Bot — Update Admin</title>
<style>
  :root {{--bg:#0d1117;--card:#161b22;--border:#30363d;--accent:#00e5ff;--green:#00e676;--red:#ff3d71;--text:#e6edf3;--dim:#8b949e}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}}
  .card{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:32px;width:100%;max-width:560px}}
  h1{{font-size:20px;font-weight:700;margin-bottom:4px;color:var(--accent)}}
  .sub{{font-size:12px;color:var(--dim);margin-bottom:28px}}
  .meta{{background:#0d1117;border:1px solid var(--border);border-radius:8px;padding:14px;margin-bottom:24px;font-size:13px;line-height:1.8}}
  .meta b{{color:var(--accent)}}
  label{{font-size:12px;color:var(--dim);display:block;margin-bottom:5px;margin-top:14px}}
  input,textarea{{width:100%;background:#0d1117;border:1px solid var(--border);color:var(--text);padding:9px 12px;border-radius:7px;font-family:inherit;font-size:13px;outline:none}}
  input:focus,textarea:focus{{border-color:var(--accent)}}
  textarea{{height:72px;resize:vertical}}
  .btn{{display:inline-block;padding:10px 20px;border:none;border-radius:7px;font-size:13px;font-weight:600;cursor:pointer;margin-top:16px;transition:.15s}}
  .btn-primary{{background:var(--accent);color:#050d12}}
  .btn-danger{{background:var(--red);color:#fff;margin-left:8px}}
  .btn:hover{{opacity:.85}}
  #result{{margin-top:16px;padding:10px 14px;border-radius:7px;font-size:13px;display:none}}
  .ok{{background:#00e67622;border:1px solid var(--green);color:var(--green)}}
  .err{{background:#ff3d7122;border:1px solid var(--red);color:var(--red)}}
  .auth-gate{{display:none}}
  .login-form{{}}
</style>
</head>
<body>
<div class="card">
  <h1>⚡ FunPay Bot — Admin Panel</h1>
  <div class="sub">Управление обновлениями</div>

  <div id="auth-section">
    <label>Admin Token</label>
    <input type="password" id="admin-token" placeholder="Введите admin token..." />
    <button class="btn btn-primary" onclick="authCheck()">Войти</button>
    <div id="result"></div>
  </div>

  <div id="main-section" style="display:none">
    <div class="meta">
      <div><b>Текущая версия:</b> <span id="m-ver">{meta['version']}</span></div>
      <div><b>Размер:</b> <span id="m-size">{size_kb} KB</span></div>
      <div><b>Загружено:</b> <span id="m-date">{uploaded}</span></div>
      <div><b>SHA256:</b> <span id="m-hash" style="font-size:10px;color:var(--dim)">{meta.get('sha256','—')[:32]}...</span></div>
      <div style="margin-top:6px;color:var(--dim);font-size:11px"><b>Changelog:</b> {meta.get('changelog','—')}</div>
    </div>

    <label>Файл обновления (.zip)</label>
    <input type="file" id="update-file" accept=".zip" />

    <label>Новая версия</label>
    <input type="text" id="new-version" placeholder="например: 1.2.0" value="" />

    <label>Changelog</label>
    <textarea id="changelog" placeholder="Что изменилось в этой версии..."></textarea>

    <div>
      <button class="btn btn-primary" onclick="uploadUpdate()">⬆ Загрузить обновление</button>
      <button class="btn btn-danger" onclick="deleteUpdate()">🗑 Удалить текущее</button>
    </div>
    <div id="result2"></div>
  </div>
</div>

<script>
let _token = '';

async function authCheck() {{
  _token = document.getElementById('admin-token').value.trim();
  const r = await fetch('/admin/status', {{headers:{{'x-admin-token': _token}}}});
  if (r.ok) {{
    document.getElementById('auth-section').style.display = 'none';
    document.getElementById('main-section').style.display = 'block';
    const d = await r.json();
    document.getElementById('m-ver').textContent = d.meta.version;
  }} else {{
    showResult('result', 'Неверный токен', false);
  }}
}}

async function uploadUpdate() {{
  const file = document.getElementById('update-file').files[0];
  if (!file) {{ showResult('result2', 'Выберите файл', false); return; }}
  const ver = document.getElementById('new-version').value.trim() || '1.0.0';
  const cl  = document.getElementById('changelog').value.trim();
  const fd = new FormData();
  fd.append('file', file);
  showResult('result2', 'Загружаю...', true);
  const r = await fetch(`/admin/upload?version=${{encodeURIComponent(ver)}}&changelog=${{encodeURIComponent(cl)}}`, {{
    method: 'POST', headers: {{'x-admin-token': _token}}, body: fd
  }});
  const d = await r.json();
  if (r.ok) {{
    showResult('result2', `Загружено v${{d.version}} (${{Math.round(d.size/1024)}} KB)`, true);
    document.getElementById('m-ver').textContent = d.version;
    document.getElementById('m-size').textContent = Math.round(d.size/1024) + ' KB';
  }} else {{
    showResult('result2', d.detail || 'Ошибка загрузки', false);
  }}
}}

async function deleteUpdate() {{
  if (!confirm('Удалить текущее обновление?')) return;
  const r = await fetch('/admin/delete', {{method:'DELETE', headers:{{'x-admin-token': _token}}}});
  const d = await r.json();
  showResult('result2', d.ok ? 'Удалено' : 'Ошибка', d.ok);
}}

function showResult(id, msg, ok) {{
  const el = document.getElementById(id);
  el.textContent = msg;
  el.className = ok ? 'ok' : 'err';
  el.style.display = 'block';
}}

document.getElementById('admin-token').addEventListener('keydown', e => {{
  if (e.key === 'Enter') authCheck();
}});
</script>
</body>
</html>"""


if __name__ == "__main__":
    print(f"[UpdateServer] Starting on port {PORT}")
    print(f"[UpdateServer] Admin panel: http://0.0.0.0:{PORT}/admin")
    print(f"[UpdateServer] ADMIN_TOKEN: {ADMIN_TOKEN}")
    print(f"[UpdateServer] ACCESS_TOKEN: {ACCESS_TOKEN}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
