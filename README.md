# Autoweb

Autoweb es una plataforma de seguimiento de noticias y registros web. Monitorea palabras clave mediante navegadores automatizados o proveedores de inteligencia artificial, evita duplicados, programa ejecuciones, mantiene un log auditable y notifica nuevos hallazgos por correo.

## Funcionalidades

- Panel con indicadores, noticias recientes, alertas y actividad.
- Gestión de palabras clave activas e inactivas.
- Monitoreo manual y automático.
- Programación diaria, semanal o mensual a una hora local.
- Búsqueda directa mediante Chrome/Google News y Edge/Bing News.
- Motores de búsqueda adicionales configurables.
- Búsqueda alternativa mediante OpenAI, Claude o Gemini.
- Deduplicación por URL exacta, URL canónica y título normalizado.
- Lista de hasta 500 noticias y actualización automática cada cinco segundos.
- Limpieza de noticias, indicadores e historial de actividad con auditoría del usuario responsable.
- Log que distingue ejecuciones manuales, automáticas y eventos de limpieza.
- Relay SMTP con correo HTML personalizado, destinatarios activos y copia administrativa BCC.
- Usuarios, roles y permisos independientes por módulo.
- Centro de alertas integrado en la interfaz.

## Tecnologías

- Python 3 y Flask.
- PostgreSQL 17 con Psycopg 3.
- Selenium 4 con Chrome y Microsoft Edge en modo headless.
- HTML, CSS y JavaScript sin framework en el cliente.
- Docker Compose para PostgreSQL local.

## Requisitos

- Python 3.10 o posterior.
- PostgreSQL accesible o Docker con Docker Compose.
- Google Chrome o Microsoft Edge para el modo Browsers.
- Acceso saliente a Internet para buscadores, APIs de IA y SMTP.

## Inicio rápido

1. Inicia PostgreSQL:

   ```powershell
   docker compose up -d postgres
   ```

   En instalaciones con Compose independiente puede utilizarse:

   ```powershell
   docker-compose up -d postgres
   ```

2. Crea y activa un entorno virtual:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

3. Instala las dependencias:

   ```powershell
   python -m pip install -r requirements.txt
   ```

4. Configura la sesión de PowerShell:

   ```powershell
   $env:DATABASE_URL="postgresql://cotelink:cotelink@127.0.0.1:5432/cotelink"
   $env:COTELINK_SECRET="una-clave-larga-y-aleatoria"
   ```

5. Inicia Autoweb:

   ```powershell
   python app.py
   ```

Abre <http://127.0.0.1:5000>.

Credenciales iniciales:

- Correo: `admin@cotelink.cl`
- Contraseña: `admin123`

Los nombres internos `cotelink`, `COTELINK_SECRET` y el correo inicial se conservan por compatibilidad con instalaciones existentes. La marca visible del producto es Autoweb.

## Variables de entorno

| Variable | Valor predeterminado | Descripción |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://cotelink:cotelink@127.0.0.1:5432/cotelink` | Conexión a PostgreSQL. |
| `COTELINK_SECRET` | `cotelink-dev-change-me` | Firma las sesiones de Flask. Debe cambiarse en producción. |

`.env.example` es solo una referencia. La aplicación no carga archivos `.env` automáticamente.

## Modos de monitoreo

### Browsers

Al habilitar **Automatización Browsers**, Autoweb omite completamente los proveedores de IA. Cada ejecución abre los navegadores correspondientes, valida los motores activos y procesa sus feeds o resultados.

Los motores iniciales son:

- Google News mediante Google Chrome.
- Bing News mediante Microsoft Edge.

Cada motor dispone de nombre, navegador, URL con el marcador `{query}` y check de activación. Selenium Manager selecciona el controlador compatible y puede requerir Internet durante el primer uso.

### Inteligencia artificial

En **APIs de IA** pueden configurarse OpenAI, Claude o Gemini. Se almacena modelo, clave y estado de activación. Si Browsers está deshabilitado, Autoweb utiliza el primer proveedor activo con clave; si ninguno está configurado, trabaja en modo demostración.

## Deduplicación

Antes de guardar un hallazgo se comprueba:

1. URL exacta.
2. URL canónica sin fragmentos ni parámetros de seguimiento como `utm_*`, `fbclid`, `gclid` o `msclkid`.
3. Título normalizado sin diferencias de mayúsculas, acentos o espacios.

Un registro existente no se inserta nuevamente, no incrementa el contador de noticias nuevas y no genera correo.

## Automatización y log

La programación admite frecuencia diaria, semanal o mensual. `next_run` conserva la hora configurada en la zona horaria local. El scheduler revisa el vencimiento cada 60 segundos y utiliza un bloqueo para impedir monitoreos simultáneos.

La interfaz consulta el estado cada cinco segundos. Durante una ejecución, el botón muestra **Ejecutando Monitoreo**, cambia a verde claro y permanece deshabilitado.

El **Log del monitoreo** registra:

- origen manual o automático;
- inicio y término;
- estado;
- resultados revisados y nuevos;
- resultados por motor;
- correos enviados o errores del relay;
- limpiezas y usuario responsable.

## Relay de correo

El menú **Relay de correo** configura:

- activación global;
- host y puerto SMTP;
- seguridad STARTTLS, SSL/TLS o sin cifrado;
- usuario y contraseña;
- nombre y correo remitente;
- correo administrativo BCC;
- envío de prueba al usuario conectado.

Cada ejecución que encuentre noticias realmente nuevas envía un único correo HTML consolidado a cada usuario activo. El mensaje reúne todos los hallazgos nuevos con título, parámetro, fuente, URL y botón de acceso. El correo administrativo recibe copia oculta de cada mensaje, salvo que sea también el destinatario principal.

Las credenciales SMTP y las claves de IA se almacenan actualmente en PostgreSQL sin cifrado adicional.

## Roles y permisos

Los permisos disponibles son:

- Resumen.
- Palabras clave.
- Noticias.
- Log del monitoreo.
- Automatización.
- Automatización Browsers.
- Relay de correo.
- Administración.

Roles iniciales:

- **Administrador:** acceso completo.
- **Analista:** monitoreo, noticias, log y automatización; sin administración ni relay.
- **Lector:** consulta del resumen y noticias.

## Limpieza de noticias

**Noticias > Limpiar noticias** solicita confirmación y elimina:

- artículos almacenados;
- contadores de noticias;
- última y próxima ejecución;
- actividad e historial de monitoreo.

Se conserva un evento de auditoría con el usuario, correo, fecha y cantidad eliminada. No se eliminan palabras clave, usuarios, roles ni configuración.

## API principal

| Ruta | Métodos | Uso |
| --- | --- | --- |
| `/api/login`, `/api/logout`, `/api/me` | POST/GET | Sesión. |
| `/api/dashboard` | GET | Indicadores, actividad y alertas. |
| `/api/keywords` | GET/POST | Lista y alta de palabras clave. |
| `/api/keywords/<id>` | PUT/DELETE | Edición y eliminación. |
| `/api/articles` | GET/DELETE | Noticias y limpieza auditada. |
| `/api/scan` | POST | Monitoreo manual. |
| `/api/runs` | GET | Log de ejecuciones. |
| `/api/schedule` | GET/PUT | Programación. |
| `/api/browser-automation` | GET/PUT | Modo Browsers. |
| `/api/browser-engines` | POST | Alta de motores. |
| `/api/browser-engines/<id>` | PUT/DELETE | Edición y eliminación de motores. |
| `/api/mail-relay` | GET/PUT | Configuración SMTP. |
| `/api/mail-relay/test` | POST | Correo de prueba. |
| `/api/password/forgot` | POST | Solicita un enlace de recuperación sin revelar si la cuenta existe. |
| `/api/password/reset` | POST | Cambia la contraseña mediante un token temporal de un solo uso. |
| `/api/settings` | GET/PUT | Proveedores de IA. |
| `/api/settings/test` | POST | Prueba de proveedor. |
| `/api/users`, `/api/roles` | GET/POST | Administración. |

Todas las rutas privadas validan sesión y permisos en el servidor.

## Pruebas automatizadas

La suite utiliza `unittest`, Flask Test Client y un esquema PostgreSQL aislado llamado `autoweb_test`. El esquema se crea nuevamente antes de cada caso y se elimina al finalizar, por lo que no modifica las tablas ni los datos de la aplicación.

Ejecutar todas las pruebas:

```powershell
python -m unittest discover -s tests -v
```

Cobertura funcional incluida:

- inicio y cierre de sesión;
- protección de endpoints;
- CRUD y validación de palabras clave;
- usuarios, roles y permisos;
- programación y cálculo horario;
- configuración de Browsers y motores;
- configuración SMTP, plantilla HTML y BCC;
- deduplicación por URL y título;
- monitoreo y origen automático;
- limpieza de noticias e indicadores;
- auditoría, alertas y log;
- normalización de URLs y títulos.

Las pruebas no abren navegadores reales, no consultan APIs externas y no envían correos: esas integraciones se sustituyen por mocks controlados.

## Estructura

```text
.
├── app.py                 # API, persistencia, scheduler, browsers y correo
├── static/
│   ├── index.html         # Interfaz
│   ├── app.js             # Cliente y actualización en tiempo real
│   └── style.css          # Estilos
├── tests/
│   └── test_app.py        # Suite funcional aislada
├── docker-compose.yml     # Autoweb y PostgreSQL (desarrollo y producción)
├── Dockerfile             # Imagen Linux con Gunicorn y Chromium
├── wsgi.py                # Inicio WSGI y scheduler único
├── requirements.txt       # Dependencias
├── .env.example           # Variables de desarrollo
└── .env.production.example# Variables de producción
```

## Despliegue en contenedores

La configuración de producción incluye Autoweb sobre Gunicorn, un scheduler único, Chromium headless, PostgreSQL 17 persistente, health checks, filesystem de aplicación de solo lectura y rotación básica de logs.

### Preparar variables

```powershell
Copy-Item .env.production.example .env.production
```

Edita `.env.production` y asigna secretos fuertes. El archivo está ignorado por Git. Si la contraseña de PostgreSQL contiene caracteres reservados de URL como `@`, `:`, `/`, `?` o `#`, deben codificarse o debe elegirse una contraseña compatible con URL.

### Construir y arrancar

```powershell
docker compose --env-file .env.production build
docker compose --env-file .env.production up -d
```

Con Compose independiente utiliza `docker-compose` en lugar de `docker compose`.

Verifica servicios y logs:

```powershell
docker compose --env-file .env.production ps
docker compose --env-file .env.production logs -f autoweb
```

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:3000/api/health
```

Respuesta esperada:

```json
{"service":"Autoweb","status":"ok"}
```

### Actualizar

```powershell
docker compose --env-file .env.production build --pull
docker compose --env-file .env.production up -d
```

El esquema se migra de manera idempotente al iniciar el contenedor.

### Respaldo

```powershell
docker compose --env-file .env.production exec -T postgres pg_dump -U autoweb -d autoweb -Fc > autoweb.backup
```

Para restaurar sobre una base vacía, detén primero Autoweb y utiliza `pg_restore`. La opción `--clean` reemplaza objetos existentes y solo debe utilizarse después de crear un respaldo.

### Detener producción

```powershell
docker compose --env-file .env.production down
```

No agregues `-v` salvo que quieras eliminar deliberadamente el volumen y todos los datos de PostgreSQL.

PostgreSQL guarda sus archivos en el volumen Docker persistente `autoweb_postgres_data`, montado en
`/var/lib/postgresql/data`. El nombre es fijo y no cambia aunque el proyecto Compose se ejecute desde otra carpeta.
Puedes comprobarlo con:

```powershell
docker volume inspect autoweb_postgres_data
```

## Producción

Antes de publicar:

- cambia `COTELINK_SECRET` y la contraseña inicial;
- almacena contraseñas de usuarios mediante hashes seguros;
- cifra claves de IA y credenciales SMTP o utiliza un gestor de secretos;
- publica Gunicorn detrás de un proxy inverso con HTTPS;
- conserva un solo worker con scheduler o separa el scheduler en un servicio exclusivo;
- restringe PostgreSQL a redes y credenciales autorizadas;
- configura respaldo, retención y monitoreo de logs;
- revisa límites SMTP, ya que cada ejecución con hallazgos genera un correo consolidado por usuario activo.

El servidor de desarrollo escucha en `127.0.0.1:5000`.

## Detener PostgreSQL local

```powershell
docker compose down
```

El volumen `cotelink_postgres_data` conserva los datos entre ejecuciones.
