import os, sqlite3, secrets, hashlib, hmac, shutil, zipfile, re
from pathlib import Path
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from jinja2 import Environment, FileSystemLoader, select_autoescape
import markdown
import ssl

BASE = Path(__file__).resolve().parent.parent
DATA = Path(os.getenv('DATA_DIR', BASE / 'data'))
PRIVATE = Path(os.getenv('PRIVATE_DIR', BASE / 'private' / 'scripts'))
UPLOADS = DATA / 'uploads'
DB = DATA / 'site.sqlite3'
DATA.mkdir(parents=True, exist_ok=True); PRIVATE.mkdir(parents=True, exist_ok=True); UPLOADS.mkdir(parents=True, exist_ok=True)
SECRET = os.getenv('SESSION_SECRET', 'CHANGE_ME_TO_A_LONG_RANDOM_SECRET')
ADMIN_USER = os.getenv('ADMIN_USER', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'change-this-password')
app = FastAPI(title='ExamPrep')
app.add_middleware(SessionMiddleware, secret_key=SECRET, https_only=os.getenv('COOKIE_HTTPS_ONLY','0')=='1', same_site='lax')
app.mount('/static', StaticFiles(directory=BASE/'app'/'static'), name='static')
app.mount('/media', StaticFiles(directory=UPLOADS), name='media')
env = Environment(loader=FileSystemLoader(BASE/'app'/'templates'), autoescape=select_autoescape(['html']))

def db():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c

def init_db():
    with db() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS articles(id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, title TEXT NOT NULL, summary TEXT DEFAULT '', public_md TEXT NOT NULL DEFAULT '', full_md TEXT NOT NULL DEFAULT '', published INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS scripts(id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, title TEXT NOT NULL, summary TEXT DEFAULT '', filename TEXT NOT NULL, public_text TEXT DEFAULT '', token TEXT UNIQUE NOT NULL, published INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS access_keys(id INTEGER PRIMARY KEY, key_hash TEXT UNIQUE NOT NULL, label TEXT DEFAULT '', expires_at TEXT, max_uses INTEGER NOT NULL DEFAULT 1, uses INTEGER NOT NULL DEFAULT 0, revoked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        ''')

def render(name, request, **ctx):
    return env.get_template(name).render(request=request, **ctx)
def now(): return datetime.now(timezone.utc).isoformat()
def hash_key(k): return hashlib.sha256(k.encode()).hexdigest()
def is_admin(req): return req.session.get('admin') is True
def has_full(req): return req.session.get('full_access') is True
def require_admin(req):
    if not is_admin(req): raise HTTPException(303, headers={'Location':'/admin/login'})
def slugify(s):
    s=s.lower().strip(); s=re.sub(r'[^a-z0-9а-яё]+','-',s); return s.strip('-')[:70] or secrets.token_hex(5)
def md(text): return markdown.markdown(text or '', extensions=['fenced_code','tables','toc','sane_lists'])

init_db()

@app.get('/', response_class=HTMLResponse)
def home(request: Request):
    with db() as c:
        articles=c.execute('SELECT * FROM articles WHERE published=1 ORDER BY id DESC').fetchall()
        scripts=c.execute('SELECT * FROM scripts WHERE published=1 ORDER BY id DESC').fetchall()
    return render('home.html',request,articles=articles,scripts=scripts,full=has_full(request))

@app.get('/article/{slug}', response_class=HTMLResponse)
def article(request: Request, slug: str):
    with db() as c: a=c.execute('SELECT * FROM articles WHERE slug=? AND published=1',(slug,)).fetchone()
    if not a: raise HTTPException(404,'Статья не найдена')
    full=has_full(request)
    body=a['full_md'] if full and a['full_md'].strip() else a['public_md']
    locked=bool(a['full_md'].strip()) and not full
    return render('article.html',request,article=a,body=md(body),full=full,locked=locked)

@app.get('/unlock', response_class=HTMLResponse)
def unlock_page(request: Request): return render('unlock.html',request,error=None)
@app.post('/unlock')
def unlock(request: Request, key: str=Form(...)):
    key=key.strip()
    with db() as c:
        row=c.execute('SELECT * FROM access_keys WHERE key_hash=? AND revoked=0',(hash_key(key),)).fetchone()
        if not row: return HTMLResponse(render('unlock.html',request,error='Ключ неверный или отключён.'),status_code=400)
        if row['expires_at'] and datetime.fromisoformat(row['expires_at']) < datetime.now(timezone.utc):
            return HTMLResponse(render('unlock.html',request,error='Срок действия ключа истёк.'),status_code=400)
        if row['uses'] >= row['max_uses']:
            return HTMLResponse(render('unlock.html',request,error='Лимит активаций этого ключа исчерпан.'),status_code=400)
        c.execute('UPDATE access_keys SET uses=uses+1 WHERE id=?',(row['id'],))
    request.session['full_access']=True
    return RedirectResponse('/',status_code=303)
@app.post('/lock')
def lock(request: Request):
    request.session.pop('full_access',None); return RedirectResponse('/',status_code=303)

@app.get('/script/{token}')
def get_script(request: Request, token: str):
    with db() as c: s=c.execute('SELECT * FROM scripts WHERE token=? AND published=1',(token,)).fetchone()
    if not s: raise HTTPException(404,'Скрипт не найден')
    if not has_full(request): return RedirectResponse('/unlock',status_code=303)
    path=PRIVATE / s['filename']
    if not path.is_file(): raise HTTPException(404,'Файл недоступен')
    return FileResponse(path, filename=s['filename'], media_type='application/octet-stream')

@app.get('/admin/login',response_class=HTMLResponse)
def admin_login(request: Request): return render('admin_login.html',request,error=None)
@app.post('/admin/login')
def admin_login_post(request: Request, username: str=Form(...), password: str=Form(...)):
    if hmac.compare_digest(username,ADMIN_USER) and hmac.compare_digest(password,ADMIN_PASSWORD):
        request.session['admin']=True; return RedirectResponse('/admin',status_code=303)
    return HTMLResponse(render('admin_login.html',request,error='Неверный логин или пароль'),status_code=401)
@app.post('/admin/logout')
def admin_logout(request: Request): request.session.clear(); return RedirectResponse('/',status_code=303)
@app.get('/admin',response_class=HTMLResponse)
def admin(request: Request):
    require_admin(request)
    with db() as c:
        articles=c.execute('SELECT * FROM articles ORDER BY id DESC').fetchall(); scripts=c.execute('SELECT * FROM scripts ORDER BY id DESC').fetchall(); keys=c.execute('SELECT * FROM access_keys ORDER BY id DESC').fetchall()
    return render('admin.html',request,articles=articles,scripts=scripts,keys=keys,new_key=request.session.pop('new_key',None))

@app.post('/admin/article/save')
def save_article(request: Request, title: str=Form(...), slug: str=Form(''), summary: str=Form(''), public_md: str=Form(''), full_md: str=Form(''), published: str | None=Form(None), article_id: int=Form(0)):
    require_admin(request); slug=slugify(slug or title); pub=1 if published=='on' else 0
    with db() as c:
        if article_id: c.execute('UPDATE articles SET slug=?,title=?,summary=?,public_md=?,full_md=?,published=?,updated_at=? WHERE id=?',(slug,title,summary,public_md,full_md,pub,now(),article_id))
        else: c.execute('INSERT INTO articles(slug,title,summary,public_md,full_md,published,updated_at) VALUES(?,?,?,?,?,?,?)',(slug,title,summary,public_md,full_md,pub,now()))
    return RedirectResponse('/admin',status_code=303)
@app.post('/admin/article/delete/{aid}')
def delete_article(request: Request, aid:int):
    require_admin(request)
    with db() as c: c.execute('DELETE FROM articles WHERE id=?',(aid,))
    return RedirectResponse('/admin',status_code=303)

@app.post('/admin/script/save')
async def save_script(request: Request, title: str=Form(...), summary: str=Form(''), public_text: str=Form(''), script_file: UploadFile=File(...), script_id: int=Form(0)):
    require_admin(request)
    if not script_file.filename or '/' in script_file.filename or '\\' in script_file.filename: raise HTTPException(400,'Недопустимое имя файла')
    content=await script_file.read()
    if len(content)>5*1024*1024: raise HTTPException(413,'Максимальный размер скрипта — 5 МБ')
    with db() as c:
        if script_id:
            old=c.execute('SELECT * FROM scripts WHERE id=?',(script_id,)).fetchone()
            if not old: raise HTTPException(404)
            filename=old['filename']; token=old['token']
        else: filename=secrets.token_hex(16)+'_'+Path(script_file.filename).name; token=secrets.token_urlsafe(24)
        (PRIVATE/filename).write_bytes(content)
        if script_id: c.execute('UPDATE scripts SET title=?,summary=?,filename=?,public_text=?,updated_at=? WHERE id=?',(title,summary,filename,public_text,now(),script_id))
        else: c.execute('INSERT INTO scripts(slug,title,summary,filename,public_text,token,published,updated_at) VALUES(?,?,?,?,?,?,1,?)',(slugify(title)+'-'+secrets.token_hex(3),title,summary,filename,public_text,token,now()))
    return RedirectResponse('/admin',status_code=303)
@app.post('/admin/script/delete/{sid}')
def delete_script(request: Request,sid:int):
    require_admin(request)
    with db() as c:
        s=c.execute('SELECT filename FROM scripts WHERE id=?',(sid,)).fetchone()
        if s:
            try: (PRIVATE/s['filename']).unlink(missing_ok=True)
            except OSError: pass
            c.execute('DELETE FROM scripts WHERE id=?',(sid,))
    return RedirectResponse('/admin',status_code=303)

@app.post('/admin/key/create')
def create_key(request: Request,label: str=Form(''), days: int=Form(0), max_uses: int=Form(1)):
    require_admin(request); key=secrets.token_urlsafe(18); expires=(datetime.now(timezone.utc)+timedelta(days=days)).isoformat() if days>0 else None
    with db() as c: c.execute('INSERT INTO access_keys(key_hash,label,expires_at,max_uses,uses,revoked,created_at) VALUES(?,?,?,?,0,0,?)',(hash_key(key),label,expires,max(1,min(max_uses,1000)),now()))
    request.session['new_key']=key; return RedirectResponse('/admin',status_code=303)
@app.post('/admin/key/revoke/{kid}')
def revoke_key(request: Request,kid:int):
    require_admin(request)
    with db() as c: c.execute('UPDATE access_keys SET revoked=1 WHERE id=?',(kid,))
    return RedirectResponse('/admin',status_code=303)

@app.get('/health')
def health(): return {'status':'ok'}

@app.post('/admin/import-bundle')
def import_bundle(request: Request):
    require_admin(request)
    # Import the provided repository zip from the optional /import folder, if mounted by the operator.
    zpath=BASE/'import'/'ebem-demo_full-main.zip'
    if not zpath.exists(): return RedirectResponse('/admin?import=missing',status_code=303)
    with zipfile.ZipFile(zpath) as z:
        names=z.namelist(); readme=next((n for n in names if n.endswith('/README.md')),None)
        if readme:
            text=z.read(readme).decode('utf-8','replace').replace('docs/images/','/media/')
            # Create a source-based starter article; administrator can split/edit it from the panel.
            with db() as c:
                if not c.execute('SELECT 1 FROM articles WHERE slug=?',('podgotovka-stenda',)).fetchone():
                    c.execute('INSERT INTO articles(slug,title,summary,public_md,full_md,published,updated_at) VALUES(?,?,?,?,?,?,?)',('podgotovka-stenda','Подготовка стенда','Методичка по подготовке VMware, Debian и виртуальных машин.',text[:2500]+'\n\n> Полная версия доступна по ключу.',text,1,now()))
                for n in names:
                    if n.endswith('.sh'):
                        raw=z.read(n); orig=Path(n).name; fn=secrets.token_hex(16)+'_'+orig; (PRIVATE/fn).write_bytes(raw)
                        title=orig[:-3].upper().replace('-','-')
                        if not c.execute('SELECT 1 FROM scripts WHERE title=?',(title,)).fetchone():
                            c.execute('INSERT INTO scripts(slug,title,summary,filename,public_text,token,published,updated_at) VALUES(?,?,?,?,?,?,1,?)',(slugify(title),title,'Скрипт автоматизации для соответствующей виртуальной машины.',''+fn,'# Открытая версия\n\nПолный скрипт доступен после активации ключа.',secrets.token_urlsafe(24),now()))
                    elif '/docs/images/' in n and n.lower().endswith(('.png','.jpg','.jpeg','.webp')):
                        dest=UPLOADS/Path(n).name; dest.write_bytes(z.read(n))
    return RedirectResponse('/admin',status_code=303)
