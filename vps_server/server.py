"""
FunPay Bot — Update Server
Запускается на VPS. Хранит актуальную версию и раздаёт обновления клиентам.
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
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

# ── Config ────────────────────────────────────────────────────────────────────
ADMIN_TOKEN  = os.environ.get("ADMIN_TOKEN", "change_me_secret_admin_token")
PORT         = int(os.environ.get("PORT", 9000))

UPDATES_DIR  = Path("updates")
UPDATES_DIR.mkdir(exist_ok=True)
OTP_DIR      = Path("otps")
OTP_DIR.mkdir(exist_ok=True)
TOKENS_DIR   = Path("tokens")
TOKENS_DIR.mkdir(exist_ok=True)

META_FILE    = UPDATES_DIR / "meta.json"
CURRENT_ZIP  = UPDATES_DIR / "current.zip"

OTP_TTL      = 300   # 5 минут
TOKEN_TTL    = 86400 * 30  # 30 дней

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

def save_token(token: str, ip: str = "", version: str = "1.0.0"):
    """Сохраняет fp_ токен на диск."""
    data = {"token": token, "ip": ip, "version": version, "created_at": time.time()}
    (TOKENS_DIR / f"{token}.json").write_text(json.dumps(data))

def validate_fp_token(token: str) -> tuple[bool, dict]:
    """Проверяет fp_ токен. Возвращает (valid, data)."""
    if not token or not token.startswith("fp_"):
        return False, {"error": "Invalid token format"}
    token_file = TOKENS_DIR / f"{token}.json"
    if not token_file.exists():
        return False, {"error": "Invalid access token"}
    try:
        data = json.loads(token_file.read_text())
        if time.time() - data.get("created_at", 0) > TOKEN_TTL:
            token_file.unlink()
            return False, {"error": "Token expired"}
        return True, data
    except Exception:
        return False, {"error": "Invalid token"}

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="FunPay Bot Update Server")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Client endpoints (защищены fp_ токеном) ───────────────────────────────────

@app.get("/version")
def get_version(x_token: Optional[str] = Header(None)):
    valid, data = validate_fp_token(x_token or "")
    if not valid:
        raise HTTPException(403, data.get("error", "Invalid access token"))
    meta = load_meta()
    meta["remote_version"] = meta["version"]
    return meta

@app.get("/download")
def download_update(x_token: Optional[str] = Header(None)):
    valid, data = validate_fp_token(x_token or "")
    if not valid:
        raise HTTPException(403, data.get("error", "Invalid access token"))
    if not CURRENT_ZIP.exists():
        raise HTTPException(404, "No update available")
    return FileResponse(CURRENT_ZIP, media_type="application/zip", filename="update.zip")

@app.get("/ping")
def ping():
    return {"ok": True, "time": time.time()}

# ── VPS регистрация через OTP ─────────────────────────────────────────────────

@app.post("/api/vps/register")
def vps_register(data: dict):
    otp = data.get("otp", "").upper().strip()
    ip  = data.get("ip", "")
    otp_file = OTP_DIR / f"{otp}.json"

    if not otp_file.exists():
        return {"error": "invalid_otp"}

    try:
        otp_data = json.loads(otp_file.read_text())
    except Exception:
        return {"error": "invalid_otp"}

    if time.time() - otp_data.get("created_at", 0) > OTP_TTL:
        otp_file.unlink(missing_ok=True)
        return {"error": "expired_otp"}

    # OTP одноразовый — удаляем
    otp_file.unlink(missing_ok=True)

    # Генерируем fp_ токен и сохраняем
    token = "fp_" + hashlib.sha256(f"{otp}{time.time()}".encode()).hexdigest()[:32]
    meta  = load_meta()
    save_token(token, ip=ip, version=meta["version"])

    return {"token": token, "version": meta["version"]}

@app.get("/api/version")
def api_version():
    return {"version": load_meta()["version"]}

# ── Admin endpoints ────────────────────────────────────────────────────────────

@app.post("/admin/upload")
async def upload_update(
    file: UploadFile = File(...),
    version: str = Form("1.0.0"),
    changelog: str = Form(""),
    x_admin_token: Optional[str] = Header(None)
):
    require_admin(x_admin_token)
    if not file.filename.endswith(".zip"):
        raise HTTPException(400, "Only .zip files accepted")

    tmp = UPDATES_DIR / "upload_tmp.zip"
    with open(tmp, "wb") as f:
        shutil.copyfileobj(file.file, f)

    checksum = sha256_file(tmp)
    size = tmp.stat().st_size
    tmp.rename(CURRENT_ZIP)

    meta = {"version": version, "changelog": changelog,
            "uploaded_at": time.time(), "size": size, "sha256": checksum}
    save_meta(meta)
    return {"ok": True, "version": version, "size": size, "sha256": checksum}

@app.get("/admin/status")
def admin_status(x_admin_token: Optional[str] = Header(None)):
    require_admin(x_admin_token)
    meta = load_meta()
    tokens = list(TOKENS_DIR.glob("*.json"))
    return {
        "current_version": meta["version"],
        "has_file": CURRENT_ZIP.exists(),
        "active_tokens": len(tokens),
        "meta": meta,
    }

@app.delete("/admin/delete")
def delete_update(x_admin_token: Optional[str] = Header(None)):
    require_admin(x_admin_token)
    if CURRENT_ZIP.exists():
        CURRENT_ZIP.unlink()
    save_meta({"version": "0.0.0", "changelog": "", "uploaded_at": 0, "size": 0, "sha256": ""})
    return {"ok": True}

@app.get("/admin", response_class=HTMLResponse)
def admin_panel():
    meta = load_meta()
    size_kb = round(meta.get("size", 0) / 1024, 1)
    uploaded = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(meta.get("uploaded_at", 0))) \
               if meta.get("uploaded_at") else "—"
    tokens_count = len(list(TOKENS_DIR.glob("*.json")))

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
  #result,#result2{{margin-top:16px;padding:10px 14px;border-radius:7px;font-size:13px;display:none}}
  .ok{{background:#00e67622;border:1px solid var(--green);color:var(--green)}}
  .err{{background:#ff3d7122;border:1px solid var(--red);color:var(--red)}}
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
      <div><b>Загружено:</b> {uploaded}</div>
      <div><b>Активных токенов:</b> {tokens_count}</div>
      <div style="margin-top:6px;color:var(--dim);font-size:11px"><b>Changelog:</b> {meta.get('changelog','—')}</div>
    </div>

    <label>Файл обновления (.zip)</label>
    <input type="file" id="update-file" accept=".zip" />
    <label>Новая версия</label>
    <input type="text" id="new-version" placeholder="например: 1.2.0" />
    <label>Changelog</label>
    <textarea id="changelog" placeholder="Что изменилось..."></textarea>
    <div>
      <button class="btn btn-primary" onclick="uploadUpdate()">⬆ Загрузить обновление</button>
      <button class="btn btn-danger" onclick="deleteUpdate()">🗑 Удалить</button>
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
  }} else {{ showResult('result', 'Неверный токен', false); }}
}}
async function uploadUpdate() {{
  const file = document.getElementById('update-file').files[0];
  if (!file) {{ showResult('result2', 'Выберите файл', false); return; }}
  const ver = document.getElementById('new-version').value.trim() || '1.0.0';
  const cl  = document.getElementById('changelog').value.trim();
  const fd = new FormData();
  fd.append('file', file);
  fd.append('version', ver);
  fd.append('changelog', cl);
  showResult('result2', 'Загружаю...', true);
  const r = await fetch('/admin/upload', {{method:'POST',headers:{{'x-admin-token':_token}},body:fd}});
  const d = await r.json();
  if (r.ok) {{ showResult('result2', `Загружено v${{d.version}}`, true); }}
  else {{ showResult('result2', d.detail||'Ошибка', false); }}
}}
async function deleteUpdate() {{
  if (!confirm('Удалить?')) return;
  const r = await fetch('/admin/delete', {{method:'DELETE',headers:{{'x-admin-token':_token}}}});
  const d = await r.json();
  showResult('result2', d.ok?'Удалено':'Ошибка', d.ok);
}}
function showResult(id, msg, ok) {{
  const el = document.getElementById(id);
  el.textContent = msg; el.className = ok?'ok':'err'; el.style.display='block';
}}
document.getElementById('admin-token').addEventListener('keydown', e => {{ if(e.key==='Enter') authCheck(); }});
</script>
</body>
</html>"""

if __name__ == "__main__":
    print(f"[UpdateServer] Starting on port {PORT}")
    print(f"[UpdateServer] Admin panel: http://0.0.0.0:{PORT}/admin")
    print(f"[UpdateServer] ADMIN_TOKEN: {ADMIN_TOKEN}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
