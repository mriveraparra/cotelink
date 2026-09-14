import json
import html
import calendar
import hashlib
import ipaddress
import io
import os
import re
import secrets
import socket
import smtplib
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import unicodedata
from datetime import datetime, time as clock_time, timedelta, timezone
from functools import wraps
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from flask import Flask, g, jsonify, request, session
import psycopg
from psycopg.rows import dict_row

# Desarrollado por Manuel Rivera Parra, 2026.

app = Flask(__name__, static_folder="static")
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
app.secret_key = os.environ.get("COTELINK_SECRET", "cotelink-dev-change-me")


@app.after_request
def disable_frontend_cache(response):
    if request.path=='/' or request.path.startswith('/static/'):
        response.headers['Cache-Control']='no-store, no-cache, must-revalidate, max-age=0'
    return response
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://cotelink:cotelink@127.0.0.1:5432/cotelink")
scan_lock = threading.Lock()
PROVIDER_HOSTS = frozenset({'api.openai.com','api.anthropic.com','generativelanguage.googleapis.com'})
SEARCH_HOSTS = frozenset(
    host.strip().lower().rstrip('.')
    for host in os.environ.get('AUTOWEB_ALLOWED_SEARCH_HOSTS','news.google.com,www.bing.com,duckduckgo.com').split(',')
    if host.strip()
)


def db():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


AUDIT_ACTIONS={
    'login':'Inicio de sesión','logout':'Cierre de sesión','forgot_password':'Solicitud de recuperación de contraseña',
    'reset_password':'Cambio de contraseña','keywords':'Gestión de palabras clave','keyword_item':'Gestión de palabra clave',
    'clear_articles':'Limpieza de noticias','settings':'Configuración de API','test_provider':'Prueba de proveedor de IA',
    'schedule_settings':'Configuración de automatización','mail_relay_settings':'Configuración del relay',
    'test_mail_relay':'Prueba de correo','browser_automation':'Configuración de browsers',
    'browser_engines':'Creación de motor','browser_engine_item':'Gestión de motor',
    'roles':'Creación de perfil','role_item':'Gestión de perfil','users':'Creación de usuario',
    'user_item':'Gestión de usuario','scan':'Ejecución manual del monitoreo','read_alerts':'Lectura de alertas',
}


@app.before_request
def remember_audit_user():
    g.audit_user=dict(session.get('user') or {})


@app.after_request
def record_user_movement(response):
    if request.method not in ('POST','PUT','DELETE'): return response
    try:
        user=dict(getattr(g,'audit_user',{}) or session.get('user') or {})
        if request.endpoint=='login' and not user:
            user={'email':str((request.get_json(silent=True) or {}).get('email',''))[:320]}
        action=AUDIT_ACTIONS.get(request.endpoint,request.endpoint or request.path)
        if response.status_code>=400: action=f'{action} (fallido)'
        with db() as c:
            c.execute("INSERT INTO audit_events(user_id,user_name,user_email,action,method,path,status_code,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                      (user.get('id'),str(user.get('name') or 'Usuario no autenticado')[:200],str(user.get('email') or '')[:320],action,request.method,request.path[:500],response.status_code,datetime.now(timezone.utc)))
    except Exception:
        app.logger.exception('No se pudo registrar el movimiento de auditoría')
    return response


def init_db():
    with db() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS users(id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL, role TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS keywords(id BIGSERIAL PRIMARY KEY, phrase TEXT UNIQUE NOT NULL, active BOOLEAN DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL);
        CREATE TABLE IF NOT EXISTS sources(id BIGSERIAL PRIMARY KEY, provider TEXT UNIQUE NOT NULL, api_key TEXT DEFAULT '', model TEXT, active BOOLEAN DEFAULT FALSE);
        CREATE TABLE IF NOT EXISTS settings(id SMALLINT PRIMARY KEY CHECK(id=1), frequency TEXT DEFAULT 'daily' CHECK(frequency IN ('daily','weekly','monthly')), hour TIME DEFAULT '09:00', last_run TIMESTAMPTZ, next_run TIMESTAMPTZ);
        CREATE TABLE IF NOT EXISTS browser_engines(id BIGSERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL, browser TEXT NOT NULL CHECK(browser IN ('chrome','edge')), search_url TEXT NOT NULL, active BOOLEAN DEFAULT TRUE);
        CREATE TABLE IF NOT EXISTS mail_relay(id SMALLINT PRIMARY KEY CHECK(id=1), enabled BOOLEAN NOT NULL DEFAULT FALSE, smtp_host TEXT DEFAULT '', smtp_port INTEGER DEFAULT 587, security TEXT DEFAULT 'starttls' CHECK(security IN ('none','starttls','ssl')), username TEXT DEFAULT '', password TEXT DEFAULT '', sender_name TEXT DEFAULT 'Autoweb', sender_email TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS articles(id BIGSERIAL PRIMARY KEY, url TEXT UNIQUE NOT NULL, title TEXT NOT NULL, source TEXT, keyword TEXT, published_at TEXT, found_at TIMESTAMPTZ NOT NULL, is_new BOOLEAN DEFAULT TRUE);
        CREATE TABLE IF NOT EXISTS runs(id BIGSERIAL PRIMARY KEY, started_at TIMESTAMPTZ NOT NULL, finished_at TIMESTAMPTZ, status TEXT, total INTEGER DEFAULT 0, new_count INTEGER DEFAULT 0, message TEXT);
        CREATE TABLE IF NOT EXISTS alerts(id BIGSERIAL PRIMARY KEY, type TEXT, title TEXT, message TEXT, created_at TIMESTAMPTZ NOT NULL, read BOOLEAN DEFAULT FALSE);
        CREATE TABLE IF NOT EXISTS roles(id BIGSERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL, description TEXT DEFAULT '', permissions JSONB NOT NULL DEFAULT '{}'::jsonb, system BOOLEAN DEFAULT FALSE);
        CREATE TABLE IF NOT EXISTS password_resets(id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, token_hash TEXT UNIQUE NOT NULL, expires_at TIMESTAMPTZ NOT NULL, used_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL);
        CREATE TABLE IF NOT EXISTS audit_events(id BIGSERIAL PRIMARY KEY, user_id BIGINT, user_name TEXT NOT NULL DEFAULT '', user_email TEXT NOT NULL DEFAULT '', action TEXT NOT NULL, method TEXT NOT NULL, path TEXT NOT NULL, status_code INTEGER NOT NULL, created_at TIMESTAMPTZ NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_articles_found_at ON articles(found_at DESC);
        CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_alerts_read ON alerts(read);
        CREATE INDEX IF NOT EXISTS idx_password_resets_token ON password_resets(token_hash);
        CREATE INDEX IF NOT EXISTS idx_audit_events_created_at ON audit_events(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_audit_events_user_id ON audit_events(user_id);
        """)
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE")
        c.execute("ALTER TABLE sources ADD COLUMN IF NOT EXISTS test_status TEXT NOT NULL DEFAULT 'untested'")
        c.execute("ALTER TABLE sources ADD COLUMN IF NOT EXISTS test_message TEXT DEFAULT ''")
        c.execute("ALTER TABLE sources ADD COLUMN IF NOT EXISTS last_test TIMESTAMPTZ")
        c.execute("ALTER TABLE settings ADD COLUMN IF NOT EXISTS browser_search BOOLEAN NOT NULL DEFAULT FALSE")
        c.execute("ALTER TABLE settings ADD COLUMN IF NOT EXISTS publication_date_filter BOOLEAN NOT NULL DEFAULT FALSE")
        c.execute("ALTER TABLE settings ADD COLUMN IF NOT EXISTS publication_max_age_days INTEGER NOT NULL DEFAULT 7")
        c.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS event_type TEXT NOT NULL DEFAULT 'monitoring'")
        c.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS run_source TEXT NOT NULL DEFAULT 'manual'")
        c.execute("ALTER TABLE mail_relay ADD COLUMN IF NOT EXISTS admin_email TEXT DEFAULT ''")
        c.execute("ALTER TABLE articles ADD COLUMN IF NOT EXISTS canonical_url TEXT DEFAULT ''")
        c.execute("ALTER TABLE articles ADD COLUMN IF NOT EXISTS title_key TEXT DEFAULT ''")
        c.execute("ALTER TABLE articles ADD COLUMN IF NOT EXISTS discovered_at TIMESTAMPTZ")
        c.execute("ALTER TABLE articles ADD COLUMN IF NOT EXISTS summary TEXT DEFAULT ''")
        c.execute("ALTER TABLE articles ADD COLUMN IF NOT EXISTS image_url TEXT DEFAULT ''")
        c.execute("UPDATE articles SET discovered_at=found_at WHERE discovered_at IS NULL")
        for article in c.execute("SELECT id,url,title FROM articles WHERE canonical_url='' OR title_key=''").fetchall():
            c.execute("UPDATE articles SET canonical_url=%s,title_key=%s WHERE id=%s",(canonical_article_url(article['url']),article_title_key(article['title']),article['id']))
        c.execute("UPDATE runs SET finished_at=%s,status='error',message='Ejecución interrumpida por reinicio del servidor' WHERE status='running'",(datetime.now(timezone.utc),))
        default_roles=[
            ('Administrador','Acceso completo a todos los módulos',{'dashboard':True,'keywords':True,'news':True,'logs':True,'schedule':True,'browsers':True,'relay':True,'apis':True,'administration':True},True),
            ('Analista','Gestiona monitoreos, palabras clave y noticias',{'dashboard':True,'keywords':True,'news':True,'logs':True,'schedule':True,'browsers':True,'relay':False,'apis':False,'administration':False},True),
            ('Lector','Consulta panel y noticias sin realizar cambios',{'dashboard':True,'keywords':False,'news':True,'logs':False,'schedule':False,'browsers':False,'relay':False,'apis':False,'administration':False},True)]
        for name,description,permissions,system in default_roles:
            c.execute("INSERT INTO roles(name,description,permissions,system) VALUES(%s,%s,%s,%s) ON CONFLICT(name) DO NOTHING",(name,description,json.dumps(permissions),system))
        c.execute("UPDATE roles SET permissions=jsonb_set(permissions,'{browsers}',COALESCE(permissions->'schedule','false'::jsonb),TRUE) WHERE NOT permissions ? 'browsers'")
        c.execute("UPDATE roles SET permissions=jsonb_set(permissions,'{logs}',CASE WHEN name IN ('Administrador','Analista') THEN 'true'::jsonb ELSE 'false'::jsonb END,TRUE) WHERE NOT permissions ? 'logs'")
        c.execute("UPDATE roles SET permissions=jsonb_set(permissions,'{relay}',CASE WHEN name='Administrador' THEN 'true'::jsonb ELSE 'false'::jsonb END,TRUE) WHERE NOT permissions ? 'relay'")
        c.execute("UPDATE roles SET permissions=jsonb_set(permissions,'{apis}',CASE WHEN name='Administrador' THEN 'true'::jsonb ELSE 'false'::jsonb END,TRUE) WHERE NOT permissions ? 'apis'")
        c.execute("INSERT INTO users(id,name,email,password,role) VALUES(1,'Martina Ríos','admin@cotelink.cl','admin123','Administrador') ON CONFLICT (id) DO NOTHING")
        c.execute("SELECT setval(pg_get_serial_sequence('users','id'), GREATEST((SELECT MAX(id) FROM users), 1))")
        c.execute("INSERT INTO settings(id,frequency,hour) VALUES(1,'daily','09:00') ON CONFLICT (id) DO NOTHING")
        c.execute("INSERT INTO mail_relay(id) VALUES(1) ON CONFLICT (id) DO NOTHING")
        c.execute("UPDATE mail_relay SET sender_name='Autoweb' WHERE sender_name='CoteLink'")
        for p, m in [('OpenAI','gpt-4.1-mini'),('Claude','claude-3-5-haiku-latest'),('Gemini','gemini-2.0-flash')]:
            c.execute("INSERT INTO sources(provider,model) VALUES(%s,%s) ON CONFLICT (provider) DO NOTHING", (p,m))
        for name,browser,search_url in [('Google','chrome','https://news.google.com/rss/search?q={query}&hl=es-419&gl=CL&ceid=CL:es-419'),('Microsoft Edge / Bing','edge','https://www.bing.com/news/search?q={query}&format=rss')]:
            c.execute("INSERT INTO browser_engines(name,browser,search_url) VALUES(%s,%s,%s) ON CONFLICT (name) DO NOTHING",(name,browser,search_url))
        c.execute("UPDATE browser_engines SET search_url='https://news.google.com/rss/search?q={query}&hl=es-419&gl=CL&ceid=CL:es-419' WHERE name='Google' AND search_url='https://www.google.com/search?q={query}'")
        c.execute("UPDATE browser_engines SET search_url='https://www.bing.com/news/search?q={query}&format=rss' WHERE name='Microsoft Edge / Bing' AND search_url='https://www.bing.com/search?q={query}'")
        c.execute("UPDATE sources SET model='gemini-3.6-flash',test_status='untested',test_message='Modelo actualizado automáticamente desde Gemini 2.0 Flash' WHERE provider='Gemini' AND model IN ('gemini-2.0-flash','gemini-2.0-flash-lite')")
        if c.execute("SELECT COUNT(*) AS count FROM keywords").fetchone()['count'] == 0:
            now = datetime.now(timezone.utc)
            for phrase in ['inteligencia artificial','ciberseguridad','transformación digital']:
                c.execute("INSERT INTO keywords(phrase,created_at) VALUES(%s,%s)", (phrase,now))


def auth(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("user"): return jsonify(error="Sesión requerida"), 401
        if 'permissions' not in session['user']:
            with db() as c: role=c.execute("SELECT permissions FROM roles WHERE name=%s",(session['user']['role'],)).fetchone()
            if not role: session.clear(); return jsonify(error="El rol del usuario ya no existe"),401
            user=dict(session['user']); user['permissions']=role['permissions']; session['user']=user
        return fn(*args, **kwargs)
    return wrapped


def admin_only(fn):
    @wraps(fn)
    @auth
    def wrapped(*args, **kwargs):
        if session['user'].get('role') != 'Administrador':
            return jsonify(error="No tienes permisos para administrar"), 403
        return fn(*args, **kwargs)
    return wrapped


def permission_required(permission):
    def decorator(fn):
        @wraps(fn)
        @auth
        def wrapped(*args, **kwargs):
            if not session['user'].get('permissions',{}).get(permission): return jsonify(error="Tu rol no tiene permiso para esta función"),403
            return fn(*args, **kwargs)
        return wrapped
    return decorator


def rows(query, args=()):
    with db() as c: result=c.execute(query,args).fetchall()
    return [{k:(v.strftime('%H:%M') if isinstance(v,clock_time) else v) for k,v in row.items()} for row in result]


def next_scheduled_run(reference,frequency,hour_value):
    local=reference.astimezone(); value=str(hour_value)[:5]
    try: hh,mm=map(int,value.split(':'))
    except (TypeError,ValueError): hh,mm=9,0
    candidate=datetime(local.year,local.month,local.day,hh,mm,tzinfo=local.tzinfo,fold=local.fold)
    if candidate>local: return candidate.astimezone(timezone.utc)
    if frequency=='daily': candidate+=timedelta(days=1)
    elif frequency=='weekly': candidate+=timedelta(days=7)
    else:
        year,month=local.year,local.month+1
        if month==13: year,month=year+1,1
        candidate=datetime(year,month,min(local.day,calendar.monthrange(year,month)[1]),hh,mm,tzinfo=local.tzinfo)
    return candidate.astimezone(timezone.utc)


def canonical_article_url(value):
    try:
        parsed=urllib.parse.urlsplit(value.strip())
        ignored={'fbclid','gclid','dclid','msclkid','ref','ref_src'}
        query=[(k,v) for k,v in urllib.parse.parse_qsl(parsed.query,keep_blank_values=True) if not k.lower().startswith('utm_') and k.lower() not in ignored]
        return urllib.parse.urlunsplit((parsed.scheme.lower(),parsed.netloc.lower(),parsed.path.rstrip('/') or '/',urllib.parse.urlencode(sorted(query)),''))
    except Exception: return value.strip()


def article_title_key(value):
    normalized=unicodedata.normalize('NFKD',value or '').encode('ascii','ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+',' ',normalized).strip()


def normalized_search_text(value):
    """Normalize case, accents and whitespace without joining separate fields."""
    normalized=unicodedata.normalize('NFKD',str(value or '')).encode('ascii','ignore').decode().casefold()
    return re.sub(r'\s+',' ',normalized).strip()


def is_multiword_keyword(keyword):
    return len(normalized_search_text(keyword).split())>1


def exact_phrase_in_text(keyword,text):
    """Match a phrase consecutively, with word boundaries, inside one text."""
    phrase=normalized_search_text(keyword)
    if not phrase: return False
    return re.search(r'(?<!\w)'+re.escape(phrase)+r'(?!\w)',normalized_search_text(text)) is not None


def article_matches_keyword(article,keyword):
    """Preserve legacy single-word behavior; strictly validate multiword phrases."""
    if not is_multiword_keyword(keyword): return True
    return any(exact_phrase_in_text(keyword,article.get(field,'')) for field in ('title','summary'))


def keyword_search_query(keyword):
    """Quote only multiword terms when querying upstream search engines."""
    return f'"{keyword}"' if is_multiword_keyword(keyword) else keyword


SPANISH_MONTHS={'enero':1,'febrero':2,'marzo':3,'abril':4,'mayo':5,'junio':6,
                'julio':7,'agosto':8,'septiembre':9,'setiembre':9,'octubre':10,
                'noviembre':11,'diciembre':12}


def parse_publication_date(value):
    """Parse common structured, RFC/feed and Spanish visible publication dates."""
    if isinstance(value,datetime): parsed=value
    else:
        raw=html.unescape(str(value or '')).strip()
        if not raw: return None
        parsed=None
        try: parsed=parsedate_to_datetime(raw)
        except (TypeError,ValueError,OverflowError): pass
        if parsed is None:
            try: parsed=datetime.fromisoformat(raw.replace('Z','+00:00'))
            except ValueError: pass
        if parsed is None:
            match=re.search(r'(\d{1,2})\s+de\s+([a-záéíóú]+)\s+de\s+(\d{4})(?:\s+(?:a\s+las\s+)?(\d{1,2}):(\d{2}))?',raw.lower())
            if match:
                month_name=unicodedata.normalize('NFKD',match.group(2)).encode('ascii','ignore').decode()
                month=SPANISH_MONTHS.get(month_name)
                if month:
                    parsed=datetime(int(match.group(3)),month,int(match.group(1)),int(match.group(4) or 0),int(match.group(5) or 0),tzinfo=timezone.utc)
        if parsed is None: return None
    if parsed.tzinfo is None: parsed=parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def select_publication_date(metadata, discovered_at):
    """Apply metadata precedence and fall back to discovery time."""
    for source in ('datePublished','article:published_time','time','visible'):
        parsed=parse_publication_date((metadata or {}).get(source))
        if parsed: return parsed,source
    return discovered_at,'discovered_at'


def generated_summary(value, limit=700):
    """Create a compact report summary from page metadata or extracted article text."""
    clean=re.sub(r'\s+',' ',html.unescape(str(value or ''))).strip()
    if not clean: return 'La fuente no entregó un resumen disponible para este hallazgo.'
    summary=' '.join(re.split(r'(?<=[.!?])\s+',clean)[:3]).strip()
    if len(summary)>limit: summary=summary[:limit].rsplit(' ',1)[0]+'…'
    return summary


def report_fragment(article, limit=2500):
    """Return an expanded source excerpt and a factual title-based fallback."""
    raw=str((article or {}).get('summary') or '').strip()
    if raw:
        clean=re.sub(r'\s+',' ',html.unescape(raw)).strip()
        if len(clean)>limit: clean=clean[:limit].rsplit(' ',1)[0]+'…'
        return clean
    title=re.sub(r'\s+',' ',html.unescape(str((article or {}).get('title') or 'esta noticia'))).strip()
    return f'El medio publicó una noticia titulada «{title}». Abre la fuente original para consultar el contenido completo.'


def search_result_summary(value):
    """Convert an HTML or plain-text search result excerpt into readable text."""
    without_tags=re.sub(r'<[^>]+>',' ',str(value or ''))
    return re.sub(r'\s+',' ',html.unescape(without_tags)).strip()


def validate_outbound_url(value, allowed_hosts, resolve=True):
    """Return a canonical HTTPS URL only when its destination is explicitly trusted."""
    try:
        parsed=urllib.parse.urlsplit(str(value).strip())
        host=(parsed.hostname or '').encode('idna').decode('ascii').lower().rstrip('.')
        port=parsed.port
    except (TypeError,ValueError,UnicodeError) as exc:
        raise ValueError("URL no válida") from exc
    if parsed.scheme.lower()!='https' or not host or parsed.username or parsed.password or port not in (None,443):
        raise ValueError("Solo se permiten URLs HTTPS sin credenciales y en el puerto 443")
    if host not in allowed_hosts:
        raise ValueError("El destino no pertenece a la lista de hosts permitidos")
    if resolve:
        try: addresses={item[4][0] for item in socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)}
        except socket.gaierror as exc: raise ValueError("No se pudo resolver el host configurado") from exc
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ValueError("No se permiten destinos locales, privados o reservados")
    return urllib.parse.urlunsplit(('https',parsed.netloc.lower(),parsed.path or '/',parsed.query,''))


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts):
        super().__init__(); self.allowed_hosts=allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe_url=validate_outbound_url(newurl,self.allowed_hosts)
        return super().redirect_request(req,fp,code,msg,headers,safe_url)


def safe_urlopen(url, *, data=None, headers=None, timeout=30, allowed_hosts):
    safe_url=validate_outbound_url(url,allowed_hosts)
    req=urllib.request.Request(safe_url,data=data,headers=headers or {},method='POST' if data is not None else 'GET')
    return urllib.request.build_opener(SafeRedirectHandler(allowed_hosts)).open(req,timeout=timeout)


@app.get("/")
def index(): return app.send_static_file("index.html")


@app.get("/api/health")
def health():
    try:
        with db() as c: c.execute("SELECT 1").fetchone()
        return jsonify(status="ok",service="Autoweb")
    except Exception: return jsonify(status="error",service="Autoweb"),503


@app.post("/api/login")
def login():
    data=request.json or {}
    with db() as c: u=c.execute("SELECT u.id,u.name,u.email,u.role,r.permissions FROM users u JOIN roles r ON r.name=u.role WHERE u.email=%s AND u.password=%s AND u.active=TRUE",(data.get('email'),data.get('password'))).fetchone()
    if not u: return jsonify(error="Correo o contraseña incorrectos"),401
    session['user']=dict(u); return jsonify(user=dict(u))


def password_reset_message(relay,user,reset_url):
    message=EmailMessage(); message['Subject']="Autoweb · Recuperación de contraseña"
    message['From']=f"{relay['sender_name']} <{relay['sender_email']}>"; message['To']=user['email']
    message.set_content(f"Hola {user['name']},\n\nRecibimos una solicitud para cambiar tu contraseña de Autoweb. El enlace es válido durante 30 minutos:\n\n{reset_url}\n\nSi no realizaste esta solicitud, ignora este mensaje.")
    message.add_alternative(f"""<!doctype html><html><body style="margin:0;background:#f3f6f4;font-family:Arial,sans-serif;color:#18302e"><div style="max-width:620px;margin:30px auto;background:white;border:1px solid #dfe8e4;border-radius:16px;overflow:hidden"><div style="background:#174d49;color:white;padding:28px 34px"><b style="font-size:22px">Autoweb</b><div style="color:#dcff7e;margin-top:6px">RECUPERACIÓN DE ACCESO</div></div><div style="padding:34px"><h1 style="font-size:24px">Hola {html.escape(user['name'])},</h1><p style="line-height:1.6;color:#526562">Recibimos una solicitud para cambiar tu contraseña. Este enlace es válido durante 30 minutos y solo puede utilizarse una vez.</p><a href="{html.escape(reset_url,quote=True)}" style="display:inline-block;background:#174d49;color:white;text-decoration:none;padding:13px 20px;border-radius:8px;font-weight:bold">Crear contraseña nueva →</a><p style="font-size:12px;color:#899693;margin-top:28px">Si no realizaste esta solicitud, puedes ignorar este mensaje.</p></div></div></body></html>""",subtype='html')
    return message


@app.post("/api/password/forgot")
def forgot_password():
    email=(request.json or {}).get('email','').strip().lower()
    generic="Si el correo pertenece a una cuenta activa, recibirás instrucciones para recuperar tu contraseña."
    with db() as c: user=c.execute("SELECT id,name,email FROM users WHERE lower(email)=%s AND active=TRUE",(email,)).fetchone()
    if not user: return jsonify(ok=True,message=generic)
    try:
        relay=rows("SELECT * FROM mail_relay WHERE id=1")[0]
        if not relay['enabled'] or not relay['smtp_host'] or not relay['sender_email']:
            raise RuntimeError("Relay no disponible para recuperación de contraseña")
        token=secrets.token_urlsafe(32); token_hash=hashlib.sha256(token.encode()).hexdigest(); now=datetime.now(timezone.utc)
        with db() as c:
            c.execute("DELETE FROM password_resets WHERE user_id=%s OR expires_at<%s",(user['id'],now))
            c.execute("INSERT INTO password_resets(user_id,token_hash,expires_at,created_at) VALUES(%s,%s,%s,%s)",(user['id'],token_hash,now+timedelta(minutes=30),now))
        base=os.environ.get('AUTOWEB_PUBLIC_URL',request.url_root.rstrip('/')).rstrip('/')
        with smtp_connection(relay) as server: server.send_message(password_reset_message(relay,user,f"{base}/?reset={urllib.parse.quote(token)}"))
    except Exception as exc:
        with db() as c: c.execute("INSERT INTO alerts(type,title,message,created_at) VALUES('error','Error recuperando contraseña',%s,%s)",(str(exc)[:500],datetime.now(timezone.utc)))
    return jsonify(ok=True,message=generic)


@app.post("/api/password/reset")
def reset_password():
    data=request.json or {}; token=data.get('token',''); password=data.get('password','')
    if len(password)<6: return jsonify(error="La contraseña debe tener al menos 6 caracteres"),400
    token_hash=hashlib.sha256(token.encode()).hexdigest(); now=datetime.now(timezone.utc)
    with db() as c:
        reset=c.execute("SELECT id,user_id FROM password_resets WHERE token_hash=%s AND used_at IS NULL AND expires_at>%s FOR UPDATE",(token_hash,now)).fetchone()
        if not reset: return jsonify(error="El enlace es inválido, expiró o ya fue utilizado"),400
        c.execute("UPDATE users SET password=%s WHERE id=%s",(password,reset['user_id']))
        c.execute("UPDATE password_resets SET used_at=%s WHERE id=%s",(now,reset['id']))
    session.clear(); return jsonify(ok=True,message="Contraseña actualizada. Ya puedes iniciar sesión.")


@app.post("/api/logout")
def logout(): session.clear(); return jsonify(ok=True)


@app.get("/api/me")
def me():
    user=session.get('user')
    if not user: return jsonify(user=None)
    with db() as c:
        current=c.execute("SELECT u.id,u.name,u.email,u.role,r.permissions FROM users u JOIN roles r ON r.name=u.role WHERE u.id=%s AND u.active=TRUE",(user['id'],)).fetchone()
    if not current:
        session.clear(); return jsonify(user=None)
    session['user']=current
    return jsonify(user=current)


@app.get("/api/dashboard")
@permission_required('dashboard')
def dashboard():
    with db() as c:
        total=c.execute("SELECT COUNT(*) AS count FROM articles").fetchone()['count']
        new=c.execute("SELECT COUNT(*) AS count FROM articles WHERE is_new=TRUE").fetchone()['count']
        active=c.execute("SELECT COUNT(*) AS count FROM keywords WHERE active=TRUE").fetchone()['count']
        setting=c.execute("SELECT *,to_char(hour,'HH24:MI') AS hour FROM settings WHERE id=1").fetchone()
        recent=c.execute("SELECT * FROM articles ORDER BY found_at DESC LIMIT 8").fetchall()
        runs=c.execute("SELECT * FROM runs WHERE event_type='monitoring' ORDER BY id DESC LIMIT 7").fetchall()
        alerts=c.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT 8").fetchall()
    return jsonify(total=total,new=new,active=active,setting=setting,recent=recent,runs=runs,alerts=alerts)


@app.get("/api/runs")
@permission_required('logs')
def monitoring_runs():
    return jsonify(items=rows("SELECT id,started_at,finished_at,status,total,new_count,message,event_type,run_source FROM runs ORDER BY id DESC LIMIT 200"))


@app.get("/api/audit-events")
@admin_only
def audit_events():
    user_id=request.args.get('user_id',type=int)
    date_from=str(request.args.get('date_from') or '').strip()
    date_to=str(request.args.get('date_to') or '').strip()
    for value in (date_from,date_to):
        if value:
            try: datetime.strptime(value,'%Y-%m-%d')
            except ValueError: return jsonify(error="La fecha del filtro no es válida"),400
    conditions=[]; params=[]
    if user_id: conditions.append('user_id=%s'); params.append(user_id)
    if date_from: conditions.append('created_at >= %s::date'); params.append(date_from)
    if date_to: conditions.append("created_at < (%s::date + interval '1 day')"); params.append(date_to)
    where=(' WHERE '+' AND '.join(conditions)) if conditions else ''
    items=rows("SELECT * FROM audit_events"+where+" ORDER BY id DESC LIMIT 500",tuple(params))
    users=rows("SELECT user_id,MAX(user_name) AS user_name,MAX(user_email) AS user_email FROM audit_events WHERE user_id IS NOT NULL GROUP BY user_id ORDER BY MAX(user_name)")
    return jsonify(items=items,users=users)


@app.route("/api/keywords", methods=["GET","POST"])
@permission_required('keywords')
def keywords():
    if request.method=='GET': return jsonify(items=rows("SELECT * FROM keywords ORDER BY id DESC"))
    phrase=(request.json or {}).get('phrase','').strip()
    if len(phrase)<2: return jsonify(error="Ingresa una palabra o frase válida"),400
    try:
        with db() as c: c.execute("INSERT INTO keywords(phrase,created_at) VALUES(%s,%s)",(phrase,datetime.now(timezone.utc)))
    except psycopg.errors.UniqueViolation: return jsonify(error="La frase ya existe"),409
    return jsonify(ok=True),201


@app.route("/api/keywords/<int:item_id>",methods=["PUT","DELETE"])
@permission_required('keywords')
def keyword_item(item_id):
    with db() as c:
        if request.method=='DELETE': c.execute("DELETE FROM keywords WHERE id=%s",(item_id,))
        else:
            d=request.json or {}; c.execute("UPDATE keywords SET phrase=%s,active=%s WHERE id=%s",(d.get('phrase','').strip(),bool(d.get('active',True)),item_id))
    return jsonify(ok=True)


@app.delete("/api/articles")
@permission_required('keywords')
def clear_articles():
    with db() as c:
        deleted=c.execute("DELETE FROM articles RETURNING id").fetchall()
        c.execute("DELETE FROM runs WHERE event_type='monitoring'")
        c.execute("UPDATE settings SET last_run=NULL,next_run=NULL WHERE id=1")
        now=datetime.now(timezone.utc); user=session['user']
        message=f"{user['name']} ({user['email']}) limpió {len(deleted)} registros de noticias"
        c.execute("INSERT INTO runs(started_at,finished_at,status,total,new_count,message,event_type) VALUES(%s,%s,'success',%s,0,%s,'cleanup')",(now,now,len(deleted),message))
    return jsonify(ok=True,deleted=len(deleted),message=f"Se eliminaron {len(deleted)} noticias")


@app.get("/api/articles")
@permission_required('news')
def articles_list():
    return jsonify(items=rows("SELECT * FROM articles ORDER BY found_at DESC LIMIT 500"))


@app.route("/api/settings", methods=["GET","PUT"])
@permission_required('apis')
def settings():
    if request.method=='GET': return jsonify(setting=rows("SELECT * FROM settings")[0],providers=rows("SELECT id,provider,model,active,test_status,test_message,last_test,CASE WHEN api_key='' THEN 0 ELSE 1 END configured FROM sources ORDER BY id"))
    d=request.json or {}
    if 'frequency' in d and not session['user'].get('permissions',{}).get('schedule'):
        return jsonify(error="Tu rol no tiene permiso para configurar la automatización"),403
    with db() as c:
        if 'frequency' in d:
            next_run=next_scheduled_run(datetime.now(timezone.utc),d['frequency'],d.get('hour','09:00'))
            c.execute("UPDATE settings SET frequency=%s,hour=%s,next_run=%s WHERE id=1",(d['frequency'],d.get('hour','09:00'),next_run))
        if 'provider' in d:
            key=d.get('api_key','').strip()
            c.execute("UPDATE sources SET api_key=CASE WHEN %s='' THEN api_key ELSE %s END,model=%s,active=%s,test_status=CASE WHEN %s='' THEN test_status ELSE 'untested' END,test_message=CASE WHEN %s='' THEN test_message ELSE '' END WHERE provider=%s",(key,key,d.get('model',''),bool(d.get('active')),key,key,d['provider']))
    return jsonify(ok=True,message="Configuración guardada correctamente")


@app.post("/api/settings/test")
@permission_required('apis')
def test_provider():
    provider_name=(request.json or {}).get('provider','')
    with db() as c: provider=c.execute("SELECT * FROM sources WHERE provider=%s",(provider_name,)).fetchone()
    if not provider: return jsonify(error="Proveedor no encontrado"),404
    if not provider['api_key']: return jsonify(error="Primero debes guardar una clave API"),400
    started=time.perf_counter()
    try:
        results=provider_articles(provider,"inteligencia artificial - prueba de conexión")
        elapsed=round((time.perf_counter()-started)*1000)
        message=f"Conexión válida. Consulta completada en {elapsed} ms; {len(results)} resultados recibidos."
        with db() as c: c.execute("UPDATE sources SET test_status='success',test_message=%s,last_test=%s WHERE id=%s",(message,datetime.now(timezone.utc),provider['id']))
        return jsonify(ok=True,status='success',message=message,results=len(results),elapsed_ms=elapsed)
    except Exception as exc:
        message=str(exc)[:500]
        with db() as c: c.execute("UPDATE sources SET test_status='error',test_message=%s,last_test=%s WHERE id=%s",(message,datetime.now(timezone.utc),provider['id']))
        return jsonify(error=f"La conexión falló: {message}",status='error'),502


@app.route("/api/schedule", methods=["GET","PUT"])
@permission_required('schedule')
def schedule_settings():
    if request.method=='GET': return jsonify(setting=rows("SELECT * FROM settings WHERE id=1")[0])
    d=request.json or {}
    if d.get('frequency') not in ('daily','weekly','monthly'): return jsonify(error="Frecuencia no válida"),400
    next_run=next_scheduled_run(datetime.now(timezone.utc),d['frequency'],d.get('hour','09:00'))
    with db() as c: c.execute("UPDATE settings SET frequency=%s,hour=%s,next_run=%s WHERE id=1",(d['frequency'],d.get('hour','09:00'),next_run))
    return jsonify(ok=True)


@app.route("/api/mail-relay",methods=["GET","PUT"])
@permission_required('relay')
def mail_relay_settings():
    if request.method=='GET':
        item=rows("SELECT enabled,smtp_host,smtp_port,security,username,sender_name,sender_email,admin_email,CASE WHEN password='' THEN FALSE ELSE TRUE END AS configured FROM mail_relay WHERE id=1")[0]
        return jsonify(setting=item)
    d=request.json or {}; security=d.get('security','starttls')
    try: port=int(d.get('smtp_port') or 587)
    except (TypeError,ValueError): return jsonify(error="Puerto SMTP no válido"),400
    if security not in ('none','starttls','ssl') or not 1<=port<=65535: return jsonify(error="Configuración SMTP no válida"),400
    password=d.get('password','')
    with db() as c:
        c.execute("UPDATE mail_relay SET enabled=%s,smtp_host=%s,smtp_port=%s,security=%s,username=%s,password=CASE WHEN %s='' THEN password ELSE %s END,sender_name=%s,sender_email=%s,admin_email=%s WHERE id=1",(bool(d.get('enabled')),d.get('smtp_host','').strip(),port,security,d.get('username','').strip(),password,password,d.get('sender_name','Autoweb').strip(),d.get('sender_email','').strip(),d.get('admin_email','').strip()))
    return jsonify(ok=True,message="Relay de correo actualizado")


@app.post("/api/mail-relay/test")
@permission_required('relay')
def test_mail_relay():
    relay=rows("SELECT * FROM mail_relay WHERE id=1")[0]
    if not relay['enabled']:
        return jsonify(error="El relay está deshabilitado. Actívalo y guarda la configuración antes de probar."),409
    send_mail(relay,session['user'],{'title':'Prueba de Relay Autoweb','url':'http://127.0.0.1:5000','keyword':'Configuración SMTP','source':'Autoweb'})
    return jsonify(ok=True,message=f"Correo de prueba enviado a {session['user']['email']}")


def valid_search_url(value):
    try:
        return '{query}' in value and bool(validate_outbound_url(value,SEARCH_HOSTS,resolve=False))
    except (TypeError,ValueError): return False


@app.route("/api/browser-automation", methods=["GET","PUT"])
@permission_required('browsers')
def browser_automation():
    if request.method=='GET':
        setting=rows("SELECT browser_search,publication_date_filter,publication_max_age_days FROM settings WHERE id=1")[0]
        return jsonify(enabled=setting['browser_search'],publication_date_filter=setting['publication_date_filter'],publication_max_age_days=setting['publication_max_age_days'],engines=rows("SELECT * FROM browser_engines ORDER BY id"))
    d=request.json or {}
    try: max_age=max(1,min(3650,int(d.get('publication_max_age_days',7))))
    except (TypeError,ValueError): return jsonify(error="La antigüedad máxima debe ser un número de días"),400
    with db() as c:
        c.execute("UPDATE settings SET browser_search=%s,publication_date_filter=%s,publication_max_age_days=%s WHERE id=1",(bool(d.get('enabled')),bool(d.get('publication_date_filter')),max_age))
    return jsonify(ok=True,message="Automatización por browsers actualizada")


@app.post("/api/browser-engines")
@permission_required('browsers')
def browser_engines():
    d=request.json or {}; name=d.get('name','').strip(); browser=d.get('browser','').lower(); search_url=d.get('search_url','').strip()
    if len(name)<2 or browser not in ('chrome','edge') or not valid_search_url(search_url):
        return jsonify(error="Indica nombre, navegador válido y una URL HTTPS que contenga {query}"),400
    try:
        with db() as c: c.execute("INSERT INTO browser_engines(name,browser,search_url,active) VALUES(%s,%s,%s,%s)",(name,browser,search_url,bool(d.get('active',True))))
    except psycopg.errors.UniqueViolation: return jsonify(error="Ya existe un motor con ese nombre"),409
    return jsonify(ok=True),201


@app.route("/api/browser-engines/<int:engine_id>",methods=["PUT","DELETE"])
@permission_required('browsers')
def browser_engine_item(engine_id):
    if request.method=='DELETE':
        with db() as c: c.execute("DELETE FROM browser_engines WHERE id=%s",(engine_id,))
        return jsonify(ok=True)
    d=request.json or {}; name=d.get('name','').strip(); browser=d.get('browser','').lower(); search_url=d.get('search_url','').strip()
    if len(name)<2 or browser not in ('chrome','edge') or not valid_search_url(search_url):
        return jsonify(error="Indica nombre, navegador válido y una URL HTTPS que contenga {query}"),400
    try:
        with db() as c: c.execute("UPDATE browser_engines SET name=%s,browser=%s,search_url=%s,active=%s WHERE id=%s",(name,browser,search_url,bool(d.get('active')),engine_id))
    except psycopg.errors.UniqueViolation: return jsonify(error="Ya existe un motor con ese nombre"),409
    return jsonify(ok=True)


def valid_role(name):
    with db() as c: return bool(c.execute("SELECT 1 FROM roles WHERE name=%s",(name,)).fetchone())


@app.route("/api/roles", methods=["GET","POST"])
@admin_only
def roles():
    if request.method=='GET': return jsonify(items=rows("SELECT id,name,description,permissions,system FROM roles ORDER BY system DESC,name"))
    d=request.json or {}; name=d.get('name','').strip(); description=d.get('description','').strip(); permissions=d.get('permissions',{})
    if len(name)<2: return jsonify(error="Ingresa un nombre válido para el rol"),400
    try:
        with db() as c: c.execute("INSERT INTO roles(name,description,permissions) VALUES(%s,%s,%s)",(name,description,json.dumps(permissions)))
    except psycopg.errors.UniqueViolation: return jsonify(error="Ya existe un rol con ese nombre"),409
    return jsonify(ok=True),201


@app.route("/api/roles/<int:role_id>", methods=["PUT","DELETE"])
@admin_only
def role_item(role_id):
    d=request.get_json(silent=True) or {}
    with db() as c: role=c.execute("SELECT * FROM roles WHERE id=%s",(role_id,)).fetchone()
    if not role: return jsonify(error="Rol no encontrado"),404
    if request.method=='DELETE':
        if role['system']: return jsonify(error="Los roles base del sistema no se pueden eliminar"),400
        with db() as c:
            if c.execute("SELECT 1 FROM users WHERE role=%s LIMIT 1",(role['name'],)).fetchone(): return jsonify(error="El rol está asignado a usuarios y no puede eliminarse"),400
            c.execute("DELETE FROM roles WHERE id=%s",(role_id,))
        return jsonify(ok=True)
    name=d.get('name','').strip(); permissions=d.get('permissions',{})
    if role['name']=='Administrador': name='Administrador'; permissions={'dashboard':True,'keywords':True,'news':True,'logs':True,'schedule':True,'browsers':True,'relay':True,'apis':True,'administration':True}
    try:
        with db() as c:
            c.execute("UPDATE roles SET name=%s,description=%s,permissions=%s WHERE id=%s",(name,d.get('description','').strip(),json.dumps(permissions),role_id))
            if name!=role['name']: c.execute("UPDATE users SET role=%s WHERE role=%s",(name,role['name']))
    except psycopg.errors.UniqueViolation: return jsonify(error="Ya existe un rol con ese nombre"),409
    return jsonify(ok=True)


@app.route("/api/users", methods=["GET", "POST"])
@admin_only
def users():
    if request.method == "GET":
        return jsonify(items=rows("SELECT id,name,email,role,active FROM users ORDER BY name"))
    d=request.json or {}; name=d.get('name','').strip(); email=d.get('email','').strip().lower(); password=d.get('password','')
    if len(name)<2 or '@' not in email or len(password)<6:
        return jsonify(error="Nombre, correo válido y contraseña de al menos 6 caracteres son obligatorios"),400
    if not valid_role(d.get('role')):
        return jsonify(error="Perfil no válido"),400
    try:
        with db() as c: c.execute("INSERT INTO users(name,email,password,role,active) VALUES(%s,%s,%s,%s,TRUE)",(name,email,password,d['role']))
    except psycopg.errors.UniqueViolation: return jsonify(error="Ya existe un usuario con ese correo"),409
    return jsonify(ok=True),201


@app.route("/api/users/<int:user_id>", methods=["PUT", "DELETE"])
@admin_only
def user_item(user_id):
    current=session['user']['id']; d=request.get_json(silent=True) or {}
    if request.method == "DELETE":
        if user_id == current: return jsonify(error="No puedes eliminar tu propio usuario"),400
        with db() as c: c.execute("DELETE FROM users WHERE id=%s",(user_id,))
        return jsonify(ok=True)
    if user_id == current and d.get('active') is False: return jsonify(error="No puedes desactivar tu propio usuario"),400
    if not valid_role(d.get('role')): return jsonify(error="Perfil no válido"),400
    name=d.get('name','').strip(); email=d.get('email','').strip().lower()
    if len(name)<2 or '@' not in email: return jsonify(error="Nombre y correo válidos son obligatorios"),400
    try:
        with db() as c:
            if d.get('password'):
                if len(d['password'])<6: return jsonify(error="La contraseña debe tener al menos 6 caracteres"),400
                c.execute("UPDATE users SET name=%s,email=%s,role=%s,active=%s,password=%s WHERE id=%s",(name,email,d['role'],bool(d.get('active')),d['password'],user_id))
            else: c.execute("UPDATE users SET name=%s,email=%s,role=%s,active=%s WHERE id=%s",(name,email,d['role'],bool(d.get('active')),user_id))
    except psycopg.errors.UniqueViolation: return jsonify(error="Ya existe un usuario con ese correo"),409
    return jsonify(ok=True)


def demo_articles(phrase):
    slug=re.sub(r'[^a-z0-9]+','-',phrase.lower()).strip('-')
    stamp=datetime.now().strftime('%Y%m%d')
    return [{"title":f"Nuevas tendencias sobre {phrase}","url":f"https://news.example.com/{slug}/{stamp}","source":"Monitor IA","published_at":datetime.now(timezone.utc).isoformat(),"summary":f"Resumen automático de las tendencias recientes relacionadas con {phrase}.","image_url":""}]


def provider_articles(provider, phrase):
    """Ask a configured model to search the web and return normalized JSON links."""
    search_instruction=(f'la frase exacta "{phrase}"; las palabras deben aparecer juntas y en ese orden'
                        if is_multiword_keyword(phrase) else f'la palabra "{phrase}"')
    prompt=(f'Busca noticias recientes y verificables que contengan {search_instruction}. Devuelve únicamente JSON '
            'como una lista de hasta 10 objetos con title, summary, image_url, url, source y published_at. '
            'summary debe ser un resumen informativo de 2 a 3 frases e image_url la imagen principal real. No inventes enlaces.')
    headers={'Content-Type':'application/json'}
    if provider['provider']=='OpenAI':
        url='https://api.openai.com/v1/responses'; headers['Authorization']='Bearer '+provider['api_key']
        payload={'model':provider['model'],'tools':[{'type':'web_search_preview'}],'input':prompt}
    elif provider['provider']=='Claude':
        url='https://api.anthropic.com/v1/messages'; headers.update({'x-api-key':provider['api_key'],'anthropic-version':'2023-06-01'})
        payload={'model':provider['model'],'max_tokens':1800,'tools':[{'type':'web_search_20250305','name':'web_search','max_uses':5}],'messages':[{'role':'user','content':prompt}]}
    elif provider['provider']=='Gemini':
        model=urllib.parse.quote(str(provider['model']),safe='._-')
        key=urllib.parse.quote(str(provider['api_key']),safe='')
        url=f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        payload={'contents':[{'parts':[{'text':prompt}]}],'tools':[{'google_search':{}}]}
    else:
        raise ValueError("Proveedor de IA no permitido")
    try:
        with safe_urlopen(url,data=json.dumps(payload).encode(),headers=headers,timeout=45,allowed_hosts=PROVIDER_HOSTS) as res:
            data=json.loads(res.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{provider['provider']}: HTTP {e.code} - {e.read().decode()[:240]}")
    if provider['provider']=='OpenAI':
        text=''.join(x.get('text','') for out in data.get('output',[]) for x in out.get('content',[]) if x.get('type')=='output_text')
    elif provider['provider']=='Claude':
        text=''.join(x.get('text','') for x in data.get('content',[]) if x.get('type')=='text')
    else: text=''.join(x.get('text','') for x in data.get('candidates',[{}])[0].get('content',{}).get('parts',[]))
    match=re.search(r'\[[\s\S]*\]',text)
    if not match: return []
    items=json.loads(match.group())
    return [x for x in items if isinstance(x,dict) and x.get('url') and x.get('title')]


def smtp_connection(relay):
    context=ssl.create_default_context()
    if relay['security']=='ssl': server=smtplib.SMTP_SSL(relay['smtp_host'],relay['smtp_port'],timeout=30,context=context)
    else:
        server=smtplib.SMTP(relay['smtp_host'],relay['smtp_port'],timeout=30)
        server.ehlo()
        if relay['security']=='starttls': server.starttls(context=context); server.ehlo()
    if relay['username']: server.login(relay['username'],relay['password'])
    return server


def notification_message(relay,user,article):
    title=html.escape(article.get('title','Nuevo registro')); keyword=html.escape(article.get('keyword','')); source=html.escape(article.get('source','Autoweb')); url=article.get('url','')
    message=EmailMessage(); message['Subject']=f"Autoweb · Nuevo registro: {article.get('title','Noticia')}"[:200]
    message['From']=f"{relay['sender_name']} <{relay['sender_email']}>"; message['To']=user['email']
    if relay.get('admin_email') and relay['admin_email'].lower()!=user['email'].lower(): message['Bcc']=relay['admin_email']
    message.set_content(f"Hola {user['name']},\n\nEl sistema de seguimiento web detectó un nuevo registro.\n\n{article.get('title','')}\nTema: {article.get('keyword','')}\nFuente: {article.get('source','')}\nURL: {url}\n\nAutoweb")
    message.add_alternative(f"""<!doctype html><html><body style="margin:0;background:#f3f6f4;font-family:Arial,sans-serif;color:#18302e"><div style="max-width:680px;margin:30px auto;background:white;border-radius:16px;overflow:hidden;border:1px solid #dfe8e4"><div style="background:#174d49;color:white;padding:28px 34px"><div style="font-size:22px;font-weight:bold">Autoweb</div><div style="color:#dcff7e;margin-top:6px">INTELIGENCIA QUE ANTICIPA</div></div><div style="padding:34px"><h1 style="font-size:25px;margin:0 0 14px">Hola {html.escape(user['name'])},</h1><p style="font-size:16px;line-height:1.6;color:#526562">El sistema de seguimiento web ha detectado un nuevo registro relacionado con tus parámetros de monitoreo.</p><div style="border:1px solid #dfe8e4;border-radius:12px;padding:22px;margin:24px 0"><div style="font-size:11px;color:#71817d;text-transform:uppercase">Nuevo hallazgo</div><h2 style="font-size:20px;line-height:1.35">{title}</h2><p><b>Parámetro:</b> {keyword}<br><b>Fuente:</b> {source}</p><a href="{html.escape(url,quote=True)}" style="display:inline-block;background:#174d49;color:white;text-decoration:none;padding:12px 18px;border-radius:8px;font-weight:bold">Ver registro completo →</a></div><p style="font-size:12px;color:#899693">Este mensaje fue enviado automáticamente por Autoweb.</p></div></div></body></html>""",subtype='html')
    return message


def send_mail(relay,user,article):
    if not relay['smtp_host'] or not relay['sender_email']: raise RuntimeError("Completa el servidor SMTP y el correo remitente")
    with smtp_connection(relay) as server: server.send_message(notification_message(relay,user,article))


def fetch_report_image(url):
    """Download a bounded public image; failures are non-fatal for the report."""
    try:
        parsed=urllib.parse.urlsplit(str(url or '').strip())
        host=(parsed.hostname or '').encode('idna').decode('ascii').lower().rstrip('.')
        if not host: return None
        headers={
            'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36',
            'Accept':'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
            'Referer':f'https://{host}/',
        }
        with safe_urlopen(url,headers=headers,timeout=12,allowed_hosts=frozenset({host})) as response:
            if not response.headers.get_content_type().startswith('image/'): return None
            data=response.read(5*1024*1024+1)
            return data if len(data)<=5*1024*1024 else None
    except Exception:
        return None


def build_news_report_pdf(articles, generated_at=None):
    """Build one polished PDF containing exactly the articles in this email report."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    generated_at=generated_at or datetime.now(timezone.utc)
    output=io.BytesIO(); doc=SimpleDocTemplate(output,pagesize=A4,rightMargin=1.7*cm,leftMargin=1.7*cm,topMargin=1.5*cm,bottomMargin=1.5*cm,title='Reporte de noticias Autoweb')
    styles=getSampleStyleSheet()
    title_style=ParagraphStyle('ReportTitle',parent=styles['Title'],fontName='Helvetica-Bold',fontSize=24,leading=29,textColor=colors.HexColor('#174d49'),spaceAfter=10)
    intro_style=ParagraphStyle('Intro',parent=styles['BodyText'],fontSize=11,leading=17,textColor=colors.HexColor('#526562'))
    article_title=ParagraphStyle('ArticleTitle',parent=styles['Heading2'],fontName='Helvetica-Bold',fontSize=17,leading=21,textColor=colors.HexColor('#18282f'),spaceAfter=8)
    body_style=ParagraphStyle('ArticleBody',parent=styles['BodyText'],fontSize=10.5,leading=16,textColor=colors.HexColor('#425652'),spaceAfter=12)
    link_style=ParagraphStyle('Link',parent=body_style,textColor=colors.HexColor('#174d49'),fontName='Helvetica-Bold')
    story=[Paragraph('AUTOWEB',ParagraphStyle('Brand',parent=styles['Heading3'],fontSize=10,textColor=colors.HexColor('#668129'),spaceAfter=8)),Paragraph('Reporte de noticias',title_style),Paragraph(f"{len(articles)} noticias de este monitoreo · {generated_at.astimezone().strftime('%d-%m-%Y %H:%M')}",intro_style),Spacer(1,0.8*cm)]
    for index,article in enumerate(articles):
        meta=Table([[Paragraph(html.escape(article.get('source','Autoweb')),intro_style),Paragraph(html.escape(article.get('keyword','')),intro_style)]],colWidths=[8*cm,8*cm])
        meta.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#eef5dc')),('BOX',(0,0),(-1,-1),0.5,colors.HexColor('#dce7bc')),('LEFTPADDING',(0,0),(-1,-1),9),('RIGHTPADDING',(0,0),(-1,-1),9),('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7)]))
        story.extend([meta,Spacer(1,0.35*cm),Paragraph(html.escape(article.get('title','Noticia')),article_title)])
        image_data=fetch_report_image(article.get('image_url'))
        if image_data:
            try: story.extend([Image(io.BytesIO(image_data),width=16*cm,height=8.2*cm,kind='proportional'),Spacer(1,0.35*cm)])
            except Exception: pass
        story.append(Paragraph(html.escape(report_fragment(article)),body_style))
        safe_link=html.escape(article.get('url',''),quote=True)
        story.append(Paragraph(f'<link href="{safe_link}">Abrir fuente original →</link>',link_style))
        if index<len(articles)-1: story.append(PageBreak())
    def decorate(canvas,_doc):
        canvas.saveState(); canvas.setFillColor(colors.HexColor('#174d49')); canvas.rect(0,A4[1]-0.55*cm,A4[0],0.55*cm,fill=1,stroke=0)
        canvas.setFont('Helvetica',8); canvas.setFillColor(colors.HexColor('#71817d')); canvas.drawRightString(A4[0]-1.7*cm,0.7*cm,f'Autoweb · página {_doc.page}'); canvas.restoreState()
    doc.build(story,onFirstPage=decorate,onLaterPages=decorate)
    return output.getvalue()


def digest_notification_message(relay,user,articles,report_pdf=None):
    """Build one message containing every new article found in a scan."""
    count=len(articles); plain_items=[]; html_items=[]
    for article in articles:
        url=article.get('url','')
        plain_items.append(f"{article.get('title','')}\nTema: {article.get('keyword','')}\nFuente: {article.get('source','')}\nURL: {url}")
        html_items.append(f"""<div style="border:1px solid #dfe8e4;border-radius:12px;padding:22px;margin:18px 0"><div style="font-size:11px;color:#71817d;text-transform:uppercase">Nuevo hallazgo</div><h2 style="font-size:20px;line-height:1.35">{html.escape(article.get('title','Nuevo registro'))}</h2><p><b>Parámetro:</b> {html.escape(article.get('keyword',''))}<br><b>Fuente:</b> {html.escape(article.get('source','Autoweb'))}</p><a href="{html.escape(url,quote=True)}" style="display:inline-block;background:#174d49;color:white;text-decoration:none;padding:12px 18px;border-radius:8px;font-weight:bold">Ver registro completo →</a></div>""")
    label='nuevo registro' if count==1 else 'nuevos registros'
    message=EmailMessage(); message['Subject']=f"Autoweb · {count} {label} detectados"
    message['From']=f"{relay['sender_name']} <{relay['sender_email']}>"; message['To']=user['email']
    if relay.get('admin_email') and relay['admin_email'].lower()!=user['email'].lower(): message['Bcc']=relay['admin_email']
    message.set_content(f"Hola {user['name']},\n\nEl sistema de seguimiento web detectó {count} {label}.\n\n"+'\n\n'.join(plain_items)+"\n\nAutoweb")
    message.add_alternative(f"""<!doctype html><html><body style="margin:0;background:#f3f6f4;font-family:Arial,sans-serif;color:#18302e"><div style="max-width:680px;margin:30px auto;background:white;border-radius:16px;overflow:hidden;border:1px solid #dfe8e4"><div style="background:#174d49;color:white;padding:28px 34px"><div style="font-size:22px;font-weight:bold">Autoweb</div><div style="color:#dcff7e;margin-top:6px">INTELIGENCIA QUE ANTICIPA</div></div><div style="padding:34px"><h1 style="font-size:25px;margin:0 0 14px">Hola {html.escape(user['name'])},</h1><p style="font-size:16px;line-height:1.6;color:#526562">El sistema de seguimiento web ha detectado <b>{count} {label}</b> relacionados con tus parámetros de monitoreo.</p>{''.join(html_items)}<p style="font-size:12px;color:#899693">Este mensaje fue enviado automáticamente por Autoweb.</p></div></div></body></html>""",subtype='html')
    pdf=report_pdf if report_pdf is not None else build_news_report_pdf(articles)
    message.add_attachment(pdf,maintype='application',subtype='pdf',filename=f"reporte_autoweb_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf")
    return message


def notify_new_articles(articles):
    if not articles: return 0,None
    relay=rows("SELECT * FROM mail_relay WHERE id=1")[0]
    if not relay['enabled']: return 0,"Relay deshabilitado; no se enviaron notificaciones"
    users=rows("SELECT name,email FROM users WHERE active=TRUE ORDER BY id")
    if not users: return 0,"No existen usuarios activos para recibir notificaciones"
    sent=0; errors=[]
    if not relay['smtp_host'] or not relay['sender_email']:
        return 0,"Relay habilitado sin servidor SMTP o remitente"
    try: report_pdf=build_news_report_pdf(articles)
    except Exception as exc: return 0,f"No se pudo generar el PDF del reporte: {exc}"
    for user in users:
        try:
            # Aísla cada destinatario para que un rechazo no bloquee los siguientes.
            with smtp_connection(relay) as server:
                refused=server.send_message(digest_notification_message(relay,user,articles,report_pdf)) or {}
            if user['email'] in refused:
                errors.append(f"{user['email']}: destinatario rechazado por SMTP")
            else: sent+=1
        except Exception as exc:
            errors.append(f"{user['email']}: {exc}")
    return sent,('; '.join(errors)[:500] if errors else None)


def browser_results(engine, phrases, inspect_publication=False):
    """Navigate a search engine with a headless browser and collect result links."""
    try:
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
    except ImportError as exc:
        raise RuntimeError("Selenium no está instalado. Ejecuta: python -m pip install -r requirements.txt") from exc
    browser_kind=engine['browser']
    if browser_kind=='edge' and os.environ.get('AUTOWEB_EDGE_FALLBACK','').lower()=='chrome': browser_kind='chrome'
    options=(webdriver.EdgeOptions() if browser_kind=='edge' else webdriver.ChromeOptions())
    if browser_kind=='chrome' and os.environ.get('CHROME_BIN'): options.binary_location=os.environ['CHROME_BIN']
    options.add_argument('--headless=new'); options.add_argument('--disable-gpu'); options.add_argument('--no-sandbox')
    options.add_argument('--window-size=1440,1000'); options.add_argument('--lang=es-CL')
    options.add_argument('--disable-blink-features=AutomationControlled')
    driver=None; collected={p:[] for p in phrases}
    try:
        driver=webdriver.Edge(options=options) if browser_kind=='edge' else webdriver.Chrome(options=options)
        driver.set_page_load_timeout(40)
        for phrase in phrases:
            prefix,marker,suffix=str(engine['search_url']).partition('{query}')
            if not marker: raise ValueError("La URL del motor no contiene el marcador {query}")
            search_url=validate_outbound_url(prefix+urllib.parse.quote_plus(keyword_search_query(phrase))+suffix,SEARCH_HOSTS)
            is_feed='rss' in search_url.lower() or 'format=rss' in search_url.lower()
            driver.get(search_url)
            validate_outbound_url(driver.current_url,SEARCH_HOSTS)
            selector='li.b_algo h2 a' if 'bing.' in engine['search_url'].lower() else 'a:has(h3)'
            if not is_feed:
                try: WebDriverWait(driver,10).until(lambda d: d.find_elements(By.CSS_SELECTOR,'item') or d.find_elements(By.CSS_SELECTOR,selector))
                except Exception: pass
            raw=driver.execute_script("""
                const selector=arguments[0];
                const feed=[...document.querySelectorAll('item')].map(item=>({
                    href:(item.querySelector('link')?.textContent||item.querySelector('link')?.nextSibling?.textContent||'').trim(),
                    title:(item.querySelector('title')?.textContent||'').trim(),
                    published_at:(item.querySelector('pubDate')?.textContent||'').trim(),
                    summary:(item.querySelector('description')?.textContent||'').trim()
                }));
                if(feed.length) return feed;
                let nodes=[...document.querySelectorAll(selector)];
                if(!nodes.length) nodes=[...document.querySelectorAll('a[href]')];
                return nodes.map(a=>{const box=a.closest('article,li,.b_algo,.g')||a.parentElement;const title=(a.innerText||a.textContent||'').trim().split('\\n')[0];const text=(box?.innerText||'').trim();return {href:a.href,title,summary:text.startsWith(title)?text.slice(title.length).trim():text}});
            """,selector)
            if is_feed:
                try:
                    with safe_urlopen(search_url,headers={'User-Agent':'Mozilla/5.0 CoteLink/1.0'},timeout=30,allowed_hosts=SEARCH_HOSTS) as response:
                        root=ET.fromstring(response.read())
                    raw=[{'href':node.findtext('link',''),'title':node.findtext('title',''),'published_at':node.findtext('pubDate',''),'summary':search_result_summary(node.findtext('description',''))} for node in root.findall('.//item')]
                except Exception as feed_error:
                    raise RuntimeError(f"{engine['name']}: el browser abrió la búsqueda, pero no se pudo leer el feed: {feed_error}") from feed_error
            seen=set()
            for item in raw:
                href=item.get('href',''); title=item.get('title','')
                parsed=urllib.parse.urlparse(href)
                query=urllib.parse.parse_qs(parsed.query)
                if parsed.netloc.endswith('google.com') and query.get('q'): href=query['q'][0]; parsed=urllib.parse.urlparse(href)
                if 'bing.com' in parsed.netloc.lower() and query.get('url'): href=query['url'][0]; parsed=urllib.parse.urlparse(href)
                if parsed.scheme not in ('http','https') or len(title)<8 or href in seen: continue
                if not is_feed and any(x in parsed.netloc.lower() for x in ('google.com','bing.com')): continue
                seen.add(href); collected[phrase].append({'title':title[:500],'url':href,'source':engine['name'],'published_at':item.get('published_at',''),'summary':search_result_summary(item.get('summary',''))})
                if len(collected[phrase])>=10: break
            if inspect_publication:
                for article in collected[phrase]:
                    try:
                        driver.get(article['url'])
                        metadata=driver.execute_script("""
                            const result={datePublished:'','article:published_time':'',time:'',visible:'',summary:'',image_url:''};
                            const visit=value=>{if(Array.isArray(value))value.forEach(visit);else if(value&&typeof value==='object'){if(!result.datePublished&&value.datePublished)result.datePublished=String(value.datePublished);if(!result.image_url&&value.image){const image=Array.isArray(value.image)?value.image[0]:value.image;result.image_url=typeof image==='string'?image:(image?.url||image?.contentUrl||'')}Object.values(value).forEach(visit)}};
                            document.querySelectorAll('script[type="application/ld+json"]').forEach(node=>{try{visit(JSON.parse(node.textContent))}catch(e){}});
                            result['article:published_time']=document.querySelector('meta[property="article:published_time"]')?.content||'';
                            result.time=document.querySelector('time[datetime]')?.getAttribute('datetime')||'';
                            const normalize=value=>(value||'').normalize('NFD').replace(/[\u0300-\u036f]/g,'').toLocaleLowerCase().replace(/\\s+/g,' ').trim();
                            const keyword=normalize(arguments[0]);
                            let paragraphs=[...document.querySelectorAll('article p, main p')]
                                .map(node=>(node.innerText||node.textContent||'').replace(/\\s+/g,' ').trim())
                                .filter((text,index,all)=>text.length>=40&&all.indexOf(text)===index);
                            if(!paragraphs.length) paragraphs=[...document.querySelectorAll('p')]
                                .map(node=>(node.innerText||node.textContent||'').replace(/\\s+/g,' ').trim())
                                .filter((text,index,all)=>text.length>=40&&all.indexOf(text)===index);
                            const related=new Set();
                            paragraphs.forEach((text,index)=>{if(keyword&&normalize(text).includes(keyword)){for(let i=Math.max(0,index-1);i<=Math.min(paragraphs.length-1,index+1);i++)related.add(i)}});
                            const selected=(related.size?[...related].sort((a,b)=>a-b).map(index=>paragraphs[index]):paragraphs.slice(0,4));
                            const contextual=selected.slice(0,8).join('\n\n').slice(0,3000).trim();
                            result.summary=contextual||document.querySelector('meta[name="description"]')?.content||document.querySelector('meta[property="og:description"]')?.content||'';
                            result.image_url=result.image_url||document.querySelector('meta[property="og:image"]')?.content||document.querySelector('meta[name="twitter:image"]')?.content||document.querySelector('link[rel="image_src"]')?.href||document.querySelector('article img')?.currentSrc||'';
                            const text=document.body?.innerText||'';
                            result.visible=(text.match(/(?:Publicado|Publicada|Fecha de publicación)[^\n]{0,120}/i)||[])[0]||'';
                            return result;
                        """,phrase) or {}
                        published,date_source=select_publication_date(metadata,datetime.now(timezone.utc))
                        article['published_at']=published.isoformat()
                        article['publication_date_source']=date_source
                        if metadata.get('summary'):
                            article['summary']=report_fragment({'summary':metadata.get('summary')})
                        article['image_url']=urllib.parse.urljoin(driver.current_url,metadata.get('image_url',''))
                        if driver.current_url.startswith(('http://','https://')): article['url']=driver.current_url
                    except Exception:
                        article['published_at']=datetime.now(timezone.utc).isoformat()
                        article['publication_date_source']='discovered_at'
    except Exception as exc:
        raise RuntimeError(f"{engine['name']}: no fue posible automatizar {engine['browser']}: {exc}") from exc
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass
    return collected


def run_scan(run_source='manual'):
    start=datetime.now(timezone.utc)
    with db() as c:
        c.execute("UPDATE articles SET is_new=FALSE")
        label='automática' if run_source=='automatic' else 'manual'
        run_id=c.execute("INSERT INTO runs(started_at,status,message,run_source) VALUES(%s,'running',%s,%s) RETURNING id",(start,f'Consultando fuentes · ejecución {label}',run_source)).fetchone()['id']
    new_count=0; total=0; new_articles=[]
    try:
        kws=rows("SELECT phrase FROM keywords WHERE active=TRUE")
        setting=rows("SELECT * FROM settings WHERE id=1")[0]
        browser_mode=setting['browser_search']
        configured=[] if browser_mode else rows("SELECT * FROM sources WHERE active=TRUE AND api_key<>'' ORDER BY id LIMIT 1")
        browser_found={k['phrase']:[] for k in kws}; engine_stats=[]
        if browser_mode:
            engines=rows("SELECT * FROM browser_engines WHERE active=TRUE ORDER BY id")
            if not engines: raise RuntimeError("La búsqueda por browsers está habilitada, pero no hay motores activos")
            for engine in engines:
                # La visita a cada noticia también obtiene resumen e imagen para el PDF;
                # no debe depender de que el filtro opcional por fecha esté activado.
                results=browser_results(engine,[k['phrase'] for k in kws],True)
                engine_stats.append(f"{engine['name']}: {sum(len(x) for x in results.values())}")
                for phrase,items in results.items(): browser_found[phrase].extend(items)
            if not any(browser_found.values()):
                raise RuntimeError("Los browsers no devolvieron resultados. "+'; '.join(engine_stats))
        for k in kws:
            found=browser_found[k['phrase']] if browser_mode else (provider_articles(configured[0],k['phrase']) if configured else demo_articles(k['phrase']))
            for a in found:
                if not article_matches_keyword(a,k['phrase']): continue
                total+=1
                canonical_url=canonical_article_url(a['url']); title_key=article_title_key(a['title'])
                with db() as c:
                    exists=c.execute("SELECT id FROM articles WHERE url=%s OR canonical_url=%s OR title_key=%s LIMIT 1",(a['url'],canonical_url,title_key)).fetchone()
                    if not exists:
                        source=a.get('source') or (configured[0]['provider'] if configured else 'Monitor IA')
                        discovered_at=datetime.now(timezone.utc)
                        published_at=parse_publication_date(a.get('published_at'))
                        filter_enabled=browser_mode and setting['publication_date_filter']
                        should_alert=not filter_enabled or (published_at or discovered_at)>=discovered_at-timedelta(days=setting['publication_max_age_days'])
                        stored_published=(published_at or discovered_at).isoformat()
                        summary=report_fragment(a)
                        image_url=str(a.get('image_url') or '')[:2000]
                        c.execute("INSERT INTO articles(url,title,source,keyword,published_at,found_at,discovered_at,is_new,canonical_url,title_key,summary,image_url) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",(a['url'],a['title'],source,k['phrase'],stored_published,discovered_at,discovered_at,should_alert,canonical_url,title_key,summary,image_url))
                        if should_alert:
                            new_count+=1
                            new_articles.append({'url':a['url'],'title':a['title'],'source':source,'keyword':k['phrase'],'summary':summary,'image_url':image_url,'published_at':stored_published})
        mails_sent,mail_error=notify_new_articles(new_articles)
        finish=datetime.now(timezone.utc)
        frequency=setting['frequency']
        next_run=next_scheduled_run(finish,frequency,setting['hour'])
        with db() as c:
            mode='browsers' if browser_mode else ('IA' if configured else 'demostración')
            detail=(' · '+'; '.join(engine_stats)) if browser_mode else ''
            mail_detail=(f' · {mails_sent} correos enviados' if mails_sent else '')+(f' · errores de correo: {mail_error}' if mail_error else '')
            c.execute("UPDATE runs SET finished_at=%s,status='success',total=%s,new_count=%s,message=%s WHERE id=%s",(finish,total,new_count,f'Monitoreo completado mediante {mode}{detail}{mail_detail}',run_id))
            c.execute("UPDATE settings SET last_run=%s,next_run=%s WHERE id=1",(finish,next_run))
            if new_count: c.execute("INSERT INTO alerts(type,title,message,created_at) VALUES('news','Nuevas noticias',%s,%s)",(f'Se encontraron {new_count} enlaces nuevos.',finish))
            if mail_error: c.execute("INSERT INTO alerts(type,title,message,created_at) VALUES('error','Error en relay de correo',%s,%s)",(mail_error,finish))
    except Exception as e:
        with db() as c:
            c.execute("UPDATE runs SET finished_at=%s,status='error',message=%s WHERE id=%s",(datetime.now(timezone.utc),str(e),run_id))
            c.execute("INSERT INTO alerts(type,title,message,created_at) VALUES('error','Error en el monitoreo',%s,%s)",(str(e),datetime.now(timezone.utc)))


def run_scan_guarded(run_source='manual'):
    if not scan_lock.acquire(blocking=False): return False
    try:
        run_scan(run_source); return True
    finally: scan_lock.release()


@app.post("/api/scan")
@permission_required('keywords')
def scan():
    if scan_lock.locked(): return jsonify(error="Ya existe un monitoreo en ejecución. Revisa su avance en el log."),409
    threading.Thread(target=run_scan_guarded,args=('manual',),daemon=True).start(); return jsonify(ok=True,message="Monitoreo iniciado")


@app.post("/api/alerts/read")
@auth
def read_alerts():
    with db() as c: c.execute("UPDATE alerts SET read=TRUE")
    return jsonify(ok=True)


def scheduler():
    while True:
        time.sleep(60)
        with db() as c: s=c.execute("SELECT * FROM settings WHERE id=1").fetchone()
        if not s: continue
        now=datetime.now(timezone.utc)
        if not s['next_run']:
            with db() as c: c.execute("UPDATE settings SET next_run=%s WHERE id=1",(next_scheduled_run(now,s['frequency'],s['hour']),))
            continue
        if now>=s['next_run'] and not scan_lock.locked():
            following=next_scheduled_run(now,s['frequency'],s['hour'])
            with db() as c: c.execute("UPDATE settings SET next_run=%s WHERE id=1",(following,))
            threading.Thread(target=run_scan_guarded,args=('automatic',),daemon=True).start()


if __name__ == '__main__':
    init_db(); threading.Thread(target=scheduler,daemon=True).start(); app.run(host='127.0.0.1',port=5000,debug=False)
