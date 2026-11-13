# AppkinsonServidor

Servidor de la app **Appkinson** (registro de voz, video y movimiento para pacientes con
Parkinson — GITA, Universidad de Antioquia). Recibe desde los teléfonos el perfil, los
medicamentos y las grabaciones de cada paciente, y les entrega los catálogos (tareas,
medicamentos, configuración).

Implementa el contrato de API v1 (`docs/API_CONTRATO.md` del repositorio de la app, branch `API`).

Todo se levanta con **un solo `docker compose up`**: API, base de datos, respaldos e interfaz
para la base de datos. Está pensado para convivir en el mismo servidor con la página web del
grupo ([research-site-v2](https://github.com/fredydo/research-site-v2)), usando su nginx
como entrada.

---

## 1. Arquitectura

```
Teléfono ──HTTPS──► (HTTPS de gita.udea.edu.co) ──HTTP──► nginx_proxy :80   [página web]
                                                          │  /           → node_app
                                                          │  /appkinson/ → appkinson_api:8000
                                                          ▼
                                      ┌──────── red "interna" de Appkinson ────────┐
                                      │ appkinson_api ──► appkinson_db (Postgres)  │
                                      │ appkinson_respaldo (pg_dump diario)        │
                                      │ appkinson_adminer (127.0.0.1:8080)         │
                                      └────────────────────────────────────────────┘
```

| Servicio | Imagen | Función | Puertos en el host |
|---|---|---|---|
| `db` | postgres:16-alpine | Base de datos | Ninguno |
| `api` | se construye (`api/`) | API FastAPI | Ninguno (la recibe nginx por la red compartida) |
| `adminer` | adminer:4.8.1 | Ver/editar la base de datos | `127.0.0.1:8080` |
| `respaldo` | postgres:16-alpine | `pg_dump` diario comprimido | Ninguno |
| `permisos` | alpine | Prepara la carpeta de grabaciones y termina | — |

No choca con la página web: nombres de contenedor distintos (`appkinson_*`), base de datos
propia y ningún puerto público nuevo.

### Carpetas persistentes

| Carpeta | Contenido | Respaldar |
|---|---|---|
| `persistente/postgres/` | Archivos internos de PostgreSQL | No directamente (usar `respaldos/`) |
| `persistente/datos/archivos/` | Grabaciones: `<documento>/Sesion_N/<modalidad>/<archivo>` | **Sí** |
| `persistente/respaldos/` | `appkinson_<fecha>.sql.gz`, uno diario | **Sí, en otro equipo** |

Las grabaciones se **borran del teléfono** cuando el servidor confirma su recepción: este
servidor es la única copia.

---

## 2. Instalación en el servidor

Requisitos: Docker Engine y Docker Compose v2.17+, y la página web ya levantada.

### 2.0 Acceso al repositorio (es privado)

El servidor se identifica ante GitHub con una *deploy key* (llave SSH de solo lectura, válida
únicamente para este repositorio):

```bash
# En el servidor
ssh-keygen -t ed25519 -C "servidor-gita-appkinson" -f ~/.ssh/appkinson_deploy -N ""
cat ~/.ssh/appkinson_deploy.pub      # copiar esta línea

# Hacer que git use esa llave para github.com
cat >> ~/.ssh/config <<'EOF'
Host github.com
    IdentityFile ~/.ssh/appkinson_deploy
    IdentitiesOnly yes
EOF
```

El dueño del repositorio la agrega en GitHub → *Settings → Deploy keys → Add deploy key*
(pegar la línea, **sin** marcar *Allow write access*).

```bash
# 1. Descargar
cd /opt
git clone git@github.com:Santiago6323/AppkinsonServidor.git
cd AppkinsonServidor

# 2. Configurar
cp .env.example .env
nano .env
#   APPKINSON_TOKEN=     <- openssl rand -hex 32
#   POSTGRES_PASSWORD=   <- openssl rand -hex 32
#   RED_PROXY=           <- nombre de la red del nginx de la página:
docker network ls        #    buscar la que termina en _web (p. ej. research-site-v2_web)

# 3. Levantar
docker compose up -d --build
docker compose ps        # api y db "healthy"; permisos "exited (0)"
```

### 2.1 Conectar con el nginx de la página web

En `nginx.conf` del repositorio de la página, dentro de `server { listen 80; ... }` y **antes**
de `location / { ... }`, agregar el bloque de [`nginx/appkinson.conf`](nginx/appkinson.conf).
Luego recargar nginx:

```bash
docker exec nginx_proxy nginx -t && docker exec nginx_proxy nginx -s reload
```

Comprobar (desde cualquier equipo):

```bash
curl -H "Authorization: Bearer <APPKINSON_TOKEN>" https://gita.udea.edu.co/appkinson/api/v1/estado
```

> ⚠️ **Límites del HTTPS de entrada.** Si delante del servidor hay otro proxy (el que pone el
> HTTPS de gita.udea.edu.co), también debe aceptar peticiones de **hasta 200 MB** y
> **300 s** para la ruta `/appkinson/`. Si no, los videos no subirán.

### 2.2 Configurar la app

En `local.properties` del proyecto Android (branch `API`):

```properties
appkinson.api.url=https://gita.udea.edu.co/appkinson/api/v1
appkinson.api.token=<APPKINSON_TOKEN>
```

---

## 3. Operación

| Acción | Comando |
|---|---|
| Estado | `docker compose ps` |
| Registros de la API | `docker compose logs -f api` |
| Detener / arrancar | `docker compose stop` / `docker compose start` |
| Apagar (no borra datos) | `docker compose down` |
| Actualizar | `git pull && docker compose up -d --build` |

Los contenedores se reinician solos si el servidor se reinicia (`restart: unless-stopped`).

> La contraseña de PostgreSQL queda fijada la **primera vez** que se crea
> `persistente/postgres/`. Cambiarla después en `.env` no cambia la de la base existente:
> `docker compose exec db psql -U appkinson -c "ALTER USER appkinson PASSWORD 'nueva'"`.

---

## 4. Ver y editar la base de datos (Adminer)

Adminer solo escucha en `127.0.0.1` del servidor. Desde tu PC, con un túnel SSH:

```bash
ssh -L 8080:127.0.0.1:8080 usuario@servidor
```

y abrir <http://localhost:8080>: sistema **PostgreSQL**, servidor `db`, usuario y contraseña
del `.env`, base de datos `appkinson`.

| Tabla | ¿Editable? | Uso |
|---|---|---|
| `tareas` | Sí | Tareas del protocolo. Para retirar una: `vigente = false` (no borrar) |
| `medicamentos_catalogo` | Sí | Lista del formulario de medicamentos (`dosis` p. ej. `{50,otra}`) |
| `configuracion_global` | Sí | Tareas por modalidad. Al guardar, la versión sube **sola** y la app la aplica aunque el paciente la haya cambiado |
| `pacientes`, `medicamentos_paciente`, `configuracion_paciente`, `archivos` | No | Los envía la app; se sobrescriben en cada sincronización |

Vistas de consulta: `vista_pacientes`, `vista_medicamentos`, `vista_archivos`.

Los cambios de catálogo llegan a los teléfonos en su siguiente sincronización (al abrir la
app, al terminar una jornada o cada 6 h, con Wi-Fi).

---

## 5. Respaldos

El servicio `respaldo` crea `persistente/respaldos/appkinson_<fecha>.sql.gz` al arrancar y
cada 24 h, y borra los de más de `APPKINSON_DIAS_RESPALDO` días (30 por defecto).

Restaurar (reemplaza el contenido actual):

```bash
docker compose stop api
gunzip -c persistente/respaldos/appkinson_<fecha>.sql.gz | docker compose exec -T db sh -c \
  'psql -U appkinson -d postgres -c "DROP DATABASE appkinson" -c "CREATE DATABASE appkinson OWNER appkinson" && psql -U appkinson -d appkinson'
docker compose start api
```

Las grabaciones (`persistente/datos/archivos/`) no están en ese respaldo: se respaldan
copiando la carpeta.

---

## 6. Pruebas en un PC (sin la página web)

```bash
docker network create appkinson_local          # una sola vez
cp .env.example .env
#   RED_PROXY=appkinson_local
#   APPKINSON_RUTA_BASE=
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
```

La API queda en `http://<IP-del-PC>:8000/api/v1` (documentación en `/docs`). La app debe ser
build **debug** (la única que permite HTTP) con esa URL en `local.properties`.

---

## 7. Seguridad

- `.env` contiene el token y la contraseña: no se sube a git ni se envía por correo/chat.
- La base de datos no tiene puerto en el host; Adminer solo en `127.0.0.1`.
- Docker publica puertos **saltándose el firewall (ufw)**: no agregar `ports:` a `api` en el
  servidor.
- Son datos de salud (Ley 1581 de 2012): restringir el acceso SSH, respaldar en otro equipo y
  seguir lo que indique el comité de ética.
