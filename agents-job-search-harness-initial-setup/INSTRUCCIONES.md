# Job Search Harness — Guía de uso

Versión de la app: 0.6.0 · Fases 1–5: triage visual, reglas, perfiles, Excel, CV y skills, búsqueda automática,
agentes externos (base compartida, API local y MCP), palabras clave de cada JD, análisis IA por paquetes, CV adaptado
por vacante (.docx), empresas y contactos · 0.6: etiquetas y vistas guardadas, salarios normalizados con tipo de
cambio, informe de brechas de skills, verificación de anuncios cerrados, "Borrar todos los datos", temas de color,
preferencias e interfaz en español o inglés

Esta guía explica cómo usar la aplicación día a día. Los detalles técnicos y el estado del
desarrollo están en `JOB_SEARCH_HARNESS_MASTER.md`, sección 19.

---

## 1. Qué es y qué hace hoy

Es una aplicación **local**: se abre en su navegador, pero todo se guarda en la carpeta de la
app y nada se envía a internet. Sirve para registrar vacantes, filtrarlas con reglas que usted
define, decidir cuáles seguir y llevar el control de sus aplicaciones.

Tiene dos **modos de búsqueda**, cada uno con sus propias reglas, columnas y libro de Excel:

| Modo | Objetivo |
|---|---|
| **Canadá — Visa / Permiso de trabajo** (`CANADA_VISA`) | Empleo en Canadá. Registra el apoyo del empleador al permiso de trabajo y la evidencia de CUSMA o LMIA. |
| **México — Remoto desde México** (`MEXICO_REMOTE`) | Trabajo remoto que se pueda hacer desde México. Registra el tipo de contratación y el salario. |

Las vacantes son **compartidas** entre modos. Cada modo guarda su propia evaluación, su propia
decisión (por ejemplo, descartar en México no la descarta en Canadá) y su propia aplicación.

**Todavía NO hace:**
- Buscar por sí misma en LinkedIn, Indeed, Glassdoor u otros sitios sin API pública. Para esos,
  un **agente externo** puede escribir las vacantes en la base (sección 5c) o cópielas a mano en
  *Agregar vacante*. La búsqueda automática propia cubre Amazon y empleadores con portales
  Greenhouse, Lever o Ashby (sección 5b).
- Leer CVs escaneados (PDF de imagen) ni archivos `.doc` antiguos (guárdelos como `.docx`).
- Llamar por sí misma a una IA. El análisis lo hace usted en ChatGPT/Claude (con un paquete que la app
  prepara) o su agente local (API/MCP); la app valida y guarda la respuesta (secciones 5d y 7b).
- Enviar postulaciones o correos.
- Importar backups desde otra carpeta (solo restaura los que están en `backups\`).

La app **nunca** aplica a vacantes ni envía correos por usted.

---

## 2. Abrir y cerrar la app

La app funciona en **Windows** y en **macOS**. Hay una versión empaquetada por sistema; ninguna
necesita instalar Python ni nada más (solo un navegador). Los datos son los mismos en ambos
sistemas: puede llevar su carpeta de datos de una PC a una Mac y viceversa (ver sección 14).

| Sistema | Archivo para descargar o copiar | Requisitos |
|---|---|---|
| Windows 10/11 (64 bits) | `JobSearchManager-Windows-x64.zip` | Ninguno |
| Mac con Apple Silicon (M1, M2, M3, M4…) | `JobSearchManager-macOS-arm64.zip` | Ninguno. Se compila y prueba en macOS 15; versiones anteriores sin verificar. |
| Mac con procesador Intel | `JobSearchManager-macOS-x86_64.zip` | Ninguno. Se compila y prueba en macOS 15; versiones anteriores sin verificar. |

¿Qué Mac tengo? Menú Apple → *Acerca de esta Mac*: "Chip Apple M…" = Apple Silicon;
"Procesador Intel" = Intel.

### Windows
1. Descomprima `JobSearchManager-Windows-x64.zip` (clic derecho → *Extraer todo*) donde quiera,
   por ejemplo `C:\JobSearch\` o una memoria USB. **Recomendación:** fuera de OneDrive, porque la
   sincronización puede bloquear la base de datos.
2. Haga doble clic en **`JobSearchManager.exe`**.
3. Se abre una ventana negra (consola) y su navegador en `http://127.0.0.1:8765`.
4. **No cierre la ventana negra** mientras usa la app. Para salir, ciérrela.

La primera vez, Windows puede mostrar un aviso de SmartScreen ("Windows protegió su PC") porque
el ejecutable no está firmado. Elija **Más información → Ejecutar de todas formas**.

### macOS
1. Haga doble clic en el `.zip` en Finder para descomprimirlo. Mueva la carpeta
   `JobSearchManager` a *Documentos* o al *Escritorio*, no la deje en *Descargas*.
   - Use el `.zip` para copiar entre computadoras. Copiar la carpeta ya descomprimida a una
     memoria USB con formato FAT32/exFAT puede dañarla (se pierden enlaces internos y permisos).
2. Haga doble clic en **`Iniciar JobSearchManager.command`**.
3. Se abre una ventana de **Terminal** y su navegador en `http://127.0.0.1:8765`.
4. **No cierre la ventana de Terminal** mientras usa la app. Para salir, ciérrela o presione
   `Ctrl+C` en ella.

**La primera vez** macOS bloquea la app porque no está firmada por Apple ("no se puede abrir
porque proviene de un desarrollador no identificado"):
- **macOS 15 (Sequoia) o más reciente:** intente abrirla una vez, luego vaya a
  *Configuración del Sistema → Privacidad y seguridad*, baje hasta el mensaje sobre
  "Iniciar JobSearchManager.command" y presione **Abrir de todas formas**.
- **macOS 14 o anterior:** clic derecho (o Control+clic) sobre `Iniciar JobSearchManager.command`
  → **Abrir** → **Abrir**.

Después de esa autorización, el lanzador quita la marca de "descargado de internet" solo de los
archivos de la app (no de sus datos), para que macOS no vuelva a preguntar por cada componente.

Si la ventana dice que macOS ejecuta la app "desde una ubicación temporal de solo lectura" (App
Translocation), mueva la carpeta a *Documentos* y, en Terminal, ejecute:
```
xattr -dr com.apple.quarantine ~/Documents/JobSearchManager
```

### En ambos sistemas
Si vuelve a abrir la app mientras ya está abierta, no se abre una segunda copia: se abre el
navegador en la app que ya está corriendo.

### Opciones (avanzado)
Desde una terminal en la carpeta de la app:

```
Windows:  JobSearchManager.exe --port 8800
macOS:    ./JobSearchManager --port 8800

--port 8800          usar otro puerto
--no-browser         no abrir el navegador automáticamente
--home <carpeta>     guardar los datos en otra carpeta
--run-sources        ejecutar las búsquedas de 'Descubrir' sin abrir la interfaz y salir
--sync               procesar la bandeja del agente externo (job_inbox) sin abrir la interfaz y salir
--mcp                servidor MCP por stdio para agentes locales (Claude Desktop/Code, VS Code…)
--mcp --read-only    igual, pero el agente solo puede leer
--agent-name <n>     con --mcp: nombre del agente en el Historial (por defecto "mcp")
--check-ads          verificar si los anuncios guardados siguen abiertos (solo marca) y salir
--self-check         verificar que la app lee CVs y usa su base de datos (carpeta temporal)
```

### Desde el código fuente (para desarrollo)
```
Windows:  .venv\Scripts\python -m jsh
macOS:    .venv/bin/python -m jsh
```

---

## 3. La pantalla

- **Arriba a la derecha:** el selector de **Modo** (Canadá, México o *Todas (ALL)*), el
  interruptor **Oscuro** (modo oscuro) y el botón **⟳** (leer ya los cambios del agente externo,
  sección 5c). La app recuerda su última elección.
- **Pestañas:**

| Pestaña | Para qué sirve |
|---|---|
| **Inicio** | Resumen: total de vacantes, activas, descartadas, seguimientos vencidos, embudo de aplicaciones, estado de la bandeja del agente externo y anuncios posiblemente cerrados (sección 8c). |
| **Descubrir** | Búsquedas guardadas que traen vacantes automáticamente (Amazon, Greenhouse, Lever, Ashby) (sección 5b). |
| **Activos** | Su lista de trabajo: vacantes pendientes de decidir o en proceso. Con vistas guardadas y filtros (sección 6b). |
| **Vacantes** | Tabla completa, con búsqueda, filtros, vistas guardadas, salario anual normalizado, etiquetas, orden por columnas y acciones en lote. |
| **Kanban** | Columnas por estado de aplicación. |
| **Empresas** | Empleadores: lista de seguimiento, ATS, legitimidad, evidencia, vacantes y contactos (sección 9b). |
| **Contactos** | Recruiters y hiring managers con su historial de interacciones (sección 9b). |
| **Agregar vacante** | Formulario para registrar una vacante. |
| **Skills y CV** | Importar sus CV, aprobar skills y datos, y guardar su CV maestro (sección 10). |
| **Análisis IA** | Paquetes para ChatGPT/Claude, respuestas recibidas, propuestas de CV y plantillas (sección 5d). |
| **Informes** | Brechas de skills (qué piden las vacantes y usted no tiene aprobado) y resumen de salarios normalizados (sección 11b). |
| **Reglas y Filtros** | Crear, editar, activar y probar reglas. |
| **Excel / Importar** | Exportar el libro Excel, reimportarlo e importar CSV. |
| **Historial** | Vacantes descartadas (para restaurarlas) y bitácora de todos los cambios. |
| **Configuración / Backups** | Apariencia, idioma y preferencias (sección 3b), moneda y tipos de cambio (sección 12b), verificación de anuncios (8c), ubicación de los datos, backups, filas rechazadas de la bandeja del agente, API local y MCP, idioma del CV generado y **Zona peligrosa** (borrar todo, sección 14b). |

El modo **Todas (ALL)** solo muestra la tabla de Vacantes con el estado en ambos modos. Para
trabajar (Activos, Reglas, Excel, Kanban) elija Canadá o México.

### 3b. Apariencia, idioma y preferencias
En *Configuración → Apariencia, idioma y preferencias* (se guardan en la carpeta portátil; **Guardar y aplicar**
recarga la página):

| Opción | Qué hace |
|---|---|
| **Idioma de la interfaz** | Español o English. Cambia menús, botones, avisos y etiquetas. Los textos internos de las tablas (p. ej. "filas por página") cambian al reiniciar la app. Los documentos generados (Excel, paquetes IA, CV .docx) mantienen su idioma; el idioma del CV se elige aparte. Algunos mensajes que vienen de datos o de agentes pueden seguir en español. |
| **Tema de color** | Azul (clásico), Verde bosque, Morado, Naranja atardecer, Turquesa, Grafito o Alto contraste. También con los círculos de color. |
| **Modo claro / oscuro** | Claro, Oscuro o Automático (según el sistema). El interruptor **Oscuro** de arriba también lo cambia. |
| **Tamaño del texto** | 90 %–150 % (útil en pantallas grandes o pequeñas). |
| **Densidad** | Normal o Compacta (menos espacio, más filas en pantalla). |
| **Pestaña al abrir** | La última que usó o una fija (p. ej. Activos). |
| **Filas por página** | 10, 25, 50 o 100 en las tablas. |
| **Tarjetas visibles en Activos** | 20, 50, 100 o Todas; con más, aparece **Mostrar las N restantes**. |
| **Aplicar automáticamente los cambios de agentes** | Si está activo y no hay ventanas abiertas, la pantalla se actualiza sola cuando un agente agrega datos (si no, aparece el aviso amarillo). |
| **Confirmación en acciones masivas** | Desactívela para que *Guardar/Restaurar seleccionadas* no pregunten. Eliminar y Borrar todo **siempre** piden confirmación. |
| **Atajos de teclado** | `/` lleva a la búsqueda de Activos/Vacantes; `?` muestra la ayuda de atajos. |

**Restablecer valores por defecto** vuelve a la apariencia original. Estas preferencias no se borran con
"Borrar todos los datos".

---

## 4. Flujo diario recomendado

0. **Una sola vez:** en *Skills y CV*, importe su CV y apruebe sus skills (sección 10), y en
   *Descubrir* cree sus búsquedas (sección 5b).
0b. **Cada día:** en *Descubrir*, presione **Ejecutar todas las activas** (o prográmelo, sección 5b).
1. **Elija el modo** en el que va a trabajar.
2. **Registre vacantes** (pestaña *Agregar vacante* o importación CSV/Excel).
3. **Revise *Activos*** y decida en cada tarjeta.
3b. **Opcional, con IA:** en *Análisis IA* genere un paquete con las vacantes que le interesan,
   péguelo en ChatGPT/Claude (o deje que su agente local lo haga por MCP/API) y **acepte** los
   análisis que le parezcan correctos (sección 5d). Si un agente externo trabaja por usted, presione
   **Mostrar cambios** cuando aparezca la barra amarilla.
3c. **Antes de aplicar:** en el detalle de la vacante, *CV adaptado a esta vacante* → propuesta
   automática o de la IA → apruebe solo cambios verdaderos → **Descargar .docx** (sección 7b).
4. **Aplique** en la página del empleador y registre el estado (y el contacto) en el detalle.
5. **Revise *Inicio*** para ver seguimientos vencidos.
6. De vez en cuando, **exporte a Excel** o **cree un backup**.

---

## 5. Agregar una vacante

Pestaña **Agregar vacante**. Solo el **Título** es obligatorio, pero cuanto más complete, mejor
funcionan las reglas.

| Campo | Consejo |
|---|---|
| Título, Empresa | Tal como aparecen en el anuncio. |
| URL del anuncio | Pegue el enlace. Se usa para detectar duplicados (se ignoran parámetros de rastreo como `utm_source`). |
| URL para aplicar | Si es distinta del anuncio. |
| País del puesto | Código o nombre: `CA`, `Canadá`, `MX`, `US`… |
| Remoto permitido desde | Lista separada por comas: `MX`, `LATAM`, `CA`, `WORLDWIDE`, `US`… **Muy importante en modo México.** |
| Modalidad | Remoto, Híbrido, Presencial o Desconocido. |
| Salario, Moneda, Periodo | Solo si el anuncio lo publica. No se hacen conversiones de moneda. |
| Declaración de visa/patrocinio | Copie el texto exacto del anuncio, si existe. |
| Descripción completa (JD) | Pegue la descripción completa. Se guarda una copia (snapshot) para el futuro. |

> **Deje vacío lo que no sabe.** Un dato vacío se trata como **DESCONOCIDO**: la vacante queda
> como "Verificar", no como aprobada ni rechazada.

Al guardar, la app indica si la vacante es **nueva** o si **ya existía** (duplicado). Un
duplicado actualiza los datos del anuncio pero **nunca** borra sus notas, decisiones ni
aplicaciones.

---

## 5b. Descubrir: búsqueda automática de vacantes

En la pestaña **Descubrir** guarda **búsquedas** que traen vacantes reales sin copiar y pegar.

### Fuentes disponibles
| Fuente | Qué necesita | Notas |
|---|---|---|
| **Amazon (amazon.jobs)** | Palabras clave y países | Usa la búsqueda pública del sitio de empleos de Amazon. Amazon no tiene API oficial, pero su `robots.txt` lo permite. La app consulta despacio (1 vez cada 1.5 s, máx. 500 resultados). |
| **Greenhouse** | Identificador del tablero del empleador | API oficial pública. Ej.: `boards.greenhouse.io/gitlab` → `gitlab`. |
| **Lever** | Nombre del sitio del empleador | API oficial pública. Ej.: `jobs.lever.co/palantir` → `palantir` (Europa: agregue `.eu`). |
| **Ashby** | Nombre del tablero del empleador | API oficial pública. Ej.: `jobs.ashbyhq.com/ashby` → `ashby`. |

Greenhouse, Lever y Ashby son **tableros de un solo empleador**: no buscan en todo internet.
Para cada empresa que le interese, averigüe qué portal usa: abra "Careers" en su sitio y mire la
dirección.

### Crear y ejecutar una búsqueda
1. **Nueva búsqueda** (o **Crear este ejemplo** la primera vez: Amazon · "data engineer" · Canadá).
2. Complete: fuente, nombre, identificador (si aplica), **palabras clave** separadas por coma,
   **países** con código de 2 letras (CA, MX, US…; vacío = todos), el **modo** con que se
   etiquetan las vacantes y el **máximo de resultados**.
3. Presione **Ejecutar ahora**. Las vacantes nuevas aparecen en *Activos* y *Vacantes*, ya
   evaluadas con sus reglas y comparadas con sus skills aprobados.

### Qué pasa en cada ejecución
- **Nuevas** se agregan; **existentes** se actualizan (descripción, fechas…) **sin tocar** sus
  decisiones, notas, aplicaciones ni descartes. No se crean duplicados.
- El **Historial de ejecuciones** muestra cuántas trajo, cuántas son nuevas, los errores y los
  avisos (por ejemplo "Amazon reporta 189 resultados; se trajeron 60").
- Si una fuente falla (sin internet, cambió su formato o rechazó el acceso), se registra el
  error y **no se borra ni modifica nada** de lo que ya tiene. La app nunca intenta evadir
  bloqueos ni iniciar sesión.
- **Eliminar una búsqueda** no elimina las vacantes que trajo.

### Cómo se interpreta la ubicación (importante para sus modos)
- **Amazon:** "Virtual" = remoto **solo dentro de ese país** ("US, Virtual" no sirve para México
  ni Canadá). También se guarda la **entidad legal contratante** (por ejemplo "Amazon Development
  Centre Canada ULC"), útil para permisos de trabajo ligados a un empleador.
- **Otros portales:** "Remote" sin país = **desconocido** (Verificar), nunca "mundial".
  "Remote - North America" también queda como desconocido. Lugares como "New Mexico" o
  "Panama City, FL" se reconocen como Estados Unidos.
- Que la vacante no hable de patrocinio de visa **no significa que lo ofrezca ni que lo niegue**:
  queda en *Verificar*.

### Ejecutar automáticamente (opcional)
Puede programar una ejecución diaria sin abrir la app:
- **Windows (Programador de tareas):** cree una tarea básica diaria que ejecute
  `JobSearchManager.exe` con el argumento `--run-sources`, iniciando en la carpeta de la app.
- **macOS (Terminal):** `crontab -e` y agregue, por ejemplo:
  `0 8 * * * cd ~/Documents/JobSearchManager && ./JobSearchManager --run-sources`

---

## 5c. Agente externo: otro programa llena la base de datos

Si un agente (Claude Code, ChatGPT/Codex, un script, un modelo local…) busca vacantes por usted,
puede escribirlas **directamente en la base** `data/jobsearch.db` mientras la app está abierta. La
app se encarga de validar, evitar duplicados, evaluar con sus reglas y mostrar los cambios.

**Lo que usted ve:**
- Cada pocos segundos la app revisa si hubo cambios. Si los hay, aparece una barra amarilla
  *"🔄 Cambios del agente externo: N vacante(s) nueva(s)…"*. Presione **Mostrar cambios**
  (la app no se refresca sola para no cerrarle un diálogo abierto).
- El botón **⟳** arriba a la derecha sincroniza en el momento.
- Las tarjetas muestran la insignia morada **Agente 82** cuando el agente envió un puntaje, y el
  detalle muestra su recomendación y resumen. Es información **del agente**: verifíquela.
- *Inicio* muestra cuántas filas importó o rechazó la bandeja; *Configuración / Backups* lista las
  rechazadas con el motivo y un botón **Reintentar rechazadas**.
- Puede crear reglas con los campos **Puntaje del agente externo** y **Recomendación del agente
  externo** (por ejemplo: *sumar 10 puntos si el puntaje del agente ≥ 70*).

**Lo que nunca hace el agente a través de la app:** cambiar sus notas, decisiones, descartes,
aplicaciones ni reglas. Si envía datos del modo (por ejemplo *apoyo de visa*), solo **completa**
campos vacíos o *unknown*; lo que usted ya escribió se conserva.

### Contrato para el agente (copie esta sección en las instrucciones de su agente)

**1. Escribir vacantes — tabla `job_inbox` (recomendado).** Una fila por vacante; solo `payload`
es obligatorio (JSON). Mismo `source` + `source_job_id`, o misma URL, actualiza en vez de duplicar.

```sql
INSERT INTO job_inbox (payload, source) VALUES ('{
  "title": "BI Developer",
  "company_name": "Globant",
  "canonical_url": "https://careers.example.com/jobs/123",
  "source": "linkedin", "source_job_id": "123",
  "work_mode": "remote", "remote_eligible_countries": ["MX", "LATAM"],
  "salary_min": 50000, "salary_max": 70000, "currency": "MXN", "salary_period": "month",
  "description": "Texto completo de la vacante",
  "modes": ["MEXICO_REMOTE"],
  "agent": {"score": 82, "recommendation": "apply", "summary": "Por qué encaja"}
}', 'mi-agente');
```

| Campo del JSON | Contenido |
|---|---|
| `title` | **Obligatorio.** Título del puesto. |
| `company_name`, `canonical_url`, `apply_url` | Empresa, URL del anuncio y URL para aplicar (http/https). |
| `source`, `source_job_id` | Sitio de origen e identificador allí: evitan duplicados. |
| `job_country`, `region`, `location_text` | País (ISO `CA`, `MX`, `US`… o nombre), provincia/estado, ciudad. |
| `work_mode` | `remote`, `hybrid` u `onsite` (vacío = desconocido). |
| `remote_eligible_countries` | Lista: países ISO o `LATAM`, `AMERICAS`, `WORLDWIDE`. |
| `salary_min`, `salary_max`, `currency`, `salary_period` | Números, moneda (`MXN`, `CAD`, `USD`…), periodo (`year`, `month`, `hour`). |
| `posted_date` | `AAAA-MM-DD` (también acepta `October 29, 2026`). |
| `seniority`, `employment_type`, `language`, `sponsorship_statement` | Texto libre. |
| `description` | Texto completo de la vacante (hasta 200.000 caracteres). |
| `modes` | `["CANADA_VISA"]`, `["MEXICO_REMOTE"]` o ambos (solo etiqueta; se evalúa en los dos). |
| `ad_status` | `active`, `expired` o `unknown`. Reenvíe con `expired` cuando el anuncio cierre. |
| `mode_data` | `{"CANADA_VISA": {"employer_visa_support": "yes", "lmia_evidence": "…", "cusma_evidence": "…"}, "MEXICO_REMOTE": {"contract_arrangement": "eor"}}` — solo completa campos vacíos. |
| `agent` | `{"score": 0-100, "recommendation": "…", "summary": "…"}` y cualquier otro dato del agente. |

Campos desconocidos se guardan aparte y se informan como aviso. Si una fecha, un salario o una URL
no se puede interpretar, ese campo se omite (con aviso) y la vacante se importa igual.

Después de procesar cada fila la app escribe el resultado en la misma fila:
`status` = `imported` o `error`, `result` = `created`/`updated`/`unchanged`, `job_id` y `message`
(error o avisos). Para reintentar una fila corregida: `UPDATE job_inbox SET status='pending' …`.

**Otros tipos de fila en `job_inbox`** (agregue `"type"` al JSON; sin `type` = vacante):

| `type` | Contenido del JSON | Efecto |
|---|---|---|
| `company` | `name` (obligatorio), `website`, `careers_url`, `ats_provider`, `ats_board_id`, `legitimacy` (`unknown`/`verified`/`suspicious`), `evidence`, `notes` | Crea o **completa** la empresa (nunca sobrescribe lo que usted escribió ni su lista de seguimiento). |
| `contact` | `name`, `company_name` o `company_id`, `role`, `email`, `phone`, `profile_url`, `evidence`, `established` (true/false) | Crea o completa un contacto (sin duplicar por correo o nombre+empresa). |
| `contact_event` | `contact_id`, `kind` (`email`/`call`/`message`/`meeting`/`interview`/`note`), `note`, `happened_on`, `job_id` | Registra una interacción. |
| `review` | Respuesta de análisis (protocolo `jsh-review/1`, ver 5d) | Queda **recibida**; usted la acepta o rechaza. |
| `tailoring` | Propuesta de CV (protocolo `jsh-tailoring/1`, ver 5d) | Queda **pendiente**; usted aprueba cada cambio. |
| `skills` | `mode` y `skills`: `[{"name", "category", "aliases", "evidence"}]` | Skills **propuestos** para su revisión (nunca aprobados automáticamente). |

Si una vacante enviada coincide con una que **usted eliminó**, la fila queda `imported` / `unchanged`
con el aviso "Omitida": no se vuelve a crear.

**2. Leer lo que el usuario decidió — vista `v_job_status` (solo lectura).** Una fila por vacante
y modo con `eligibility_status`, `technical_score`, `skills_match`, `user_decision`,
`discard_reason`, `discard_note`, `application_status`, `notes`, `source`, `source_job_id`…
Úsela para no volver a proponer vacantes descartadas:
```sql
SELECT source, source_job_id, canonical_url, discard_reason FROM v_job_status
WHERE user_decision = 'discarded';
```

**3. Editar directamente `jobs` (tolerado).** Puede hacer `UPDATE jobs SET description=…,
salary_min=…, ad_status=… WHERE id=…` sobre los datos del anuncio. La app repara valores con
formato raro (fechas en texto, `"50k"`, listas como `"MX, LATAM"`), vuelve a evaluar la vacante,
guarda la nueva JD como snapshot y lo registra en *Historial*. **No** modifique `notes`, `pinned`,
`row_version` ni las demás tablas; para vacantes nuevas use siempre `job_inbox` (un `INSERT`
directo en `jobs` exige todas las columnas).

**Reglas técnicas para el agente:**
- Abra la base con SQLite en la **misma computadora** (no por red) y haga transacciones cortas
  (`COMMIT` después de cada vacante o lote pequeño). Configure una espera ante bloqueo
  (`timeout=30` en Python `sqlite3.connect`, o `PRAGMA busy_timeout=30000`).
- La base usa modo **WAL**: mientras está abierta verá `jobsearch.db-wal` y `jobsearch.db-shm`
  junto a `jobsearch.db`. Son normales; al cerrar la app se integran al archivo principal.
- Si la app está cerrada, las filas quedan `pending` y se procesan al abrirla, o ejecute
  `JobSearchManager --sync` para procesarlas sin abrir la interfaz.
- Las filas de `job_inbox` se aplican **en orden**. No mezcle un `UPDATE jobs` directo con filas
  aún `pending` de la misma vacante (la fila pendiente, más antigua, se aplicaría después y
  ganaría). Lo más simple: actualice también por `job_inbox`, o espere a que no queden `pending`.
- No restaure backups mientras el agente está escribiendo.

Ejemplo mínimo en Python (sin instalar nada):
```python
import json, sqlite3
con = sqlite3.connect(r"C:\ruta\JobSearchManager\data\jobsearch.db", timeout=30)
job = {"title": "Data Engineer", "company_name": "Acme", "canonical_url": "https://acme.example/j/1",
       "work_mode": "remote", "remote_eligible_countries": ["MX"], "modes": ["MEXICO_REMOTE"],
       "agent": {"score": 75, "summary": "SQL + Python, remoto LATAM"}}
con.execute("INSERT INTO job_inbox (payload, source) VALUES (?, ?)", (json.dumps(job), "mi-agente"))
con.commit()
```

---

## 5d. Agentes locales (API y MCP) y análisis con IA de la web

La app **no llama a ninguna IA**. Hay tres formas de que una IA trabaje con sus datos; las tres
pasan por las mismas validaciones y **nada cambia sus decisiones** sin su clic.

| Quién | Cómo se conecta | Dónde se configura |
|---|---|---|
| ChatGPT / Claude **en el navegador** | Paquete `.md` que usted pega en el chat; usted pega de vuelta la respuesta JSON | Pestaña **Análisis IA** o detalle de la vacante |
| Claude Desktop, Claude Code, VS Code Copilot, Cursor… | **MCP** (el agente arranca `JobSearchManager --mcp`) | *Configuración → API local y MCP* (copiar configuración) |
| Scripts, n8n, modelos locales | **API REST** en `http://127.0.0.1:8765/api/v1` con token | *Configuración → API local y MCP* (tokens y documentación) |
| Cualquier programa con SQLite | Bandeja `job_inbox` (sección 5c) | — |

### Paquetes para ChatGPT/Claude en la web
1. En **Análisis IA**, marque vacantes (máx. 25) y presione **Generar paquete (.md + .json)**. Se
   descarga un `.md` (y queda una copia en `exports/`). Contiene: instrucciones, su perfil
   **aprobado** (skills, experiencia, sin correo ni teléfono salvo que marque la casilla), las reglas,
   cada JD en texto plano con sus palabras clave y el formato exacto de respuesta.
2. Pegue el `.md` en el chat. La IA responde con un JSON.
3. **Pegar / subir respuesta de la IA**. La app comprueba el protocolo, que cada vacante pertenezca
   al paquete y que el `jd_hash` sea el de la JD enviada (si la JD cambió, se marca *desactualizada*).
4. Cada análisis queda **Recibido**. **Aceptar análisis** lo convierte en el análisis vigente de esa
   vacante: aparece la insignia **IA 81** y alimenta los campos de reglas *IA: ajuste técnico*,
   *IA: prioridad*, *IA: recomendación*, *IA: ubicación compatible* e *IA: autorización de trabajo*.
   Si la IA sugiere descartar, verá un botón **Descartar (sugerido por la IA)**; la decisión es suya.

Para el CV adaptado, el detalle de cada vacante tiene **Paquete para IA** y **Pegar propuesta de la
IA** (sección 7b).

### MCP (Claude Desktop, Claude Code, VS Code…)
En *Configuración → API local y MCP → Configurar un cliente MCP* copie el bloque para su programa.
El agente obtiene herramientas como `search_jobs`, `get_job` (JD plana + palabras clave + coincidencia
con su CV), `get_job_markdown`, `get_user_decisions`, `get_candidate_profile`, `add_or_update_job`,
`upsert_company`, `upsert_contact`, `log_contact_event`, `get_review_package` → `submit_job_review` y
`get_tailoring_package` → `submit_resume_tailoring`, `get_resume` (su CV como fuente: CV maestro con
ids, skills aprobados/propuestos/rechazados, CV adaptados y el texto de los CV importados **sin datos de
contacto**), `get_tailored_resume` y `propose_skills` (sugerir skills; quedan como propuestas).
Agregue `--read-only` para que solo pueda leer. Ningún agente puede eliminar datos.
La app puede estar abierta o cerrada; los cambios aparecen en la barra amarilla.

### API REST local
- Base: `http://127.0.0.1:8765/api/v1` (o el puerto que muestra la ventana). Documentación
  interactiva en `/api/v1/docs`.
- Encabezado obligatorio `Authorization: Bearer <token>`. Hay un token de **lectura y escritura** y
  otro de **solo lectura**; cópielos o regenérelos en *Configuración*. Opcional `X-Agent-Name: <nombre>`
  para identificar al agente en el *Historial*.
- Solo responde a `127.0.0.1`/`localhost` y no envía encabezados CORS: páginas web no pueden leerla.
- Principales rutas: `GET /jobs?mode=…&q=…`, `GET /jobs/{id}`, `GET /jobs/{id}/jd?format=markdown|text|json`,
  `POST /jobs`, `POST /jobs/{id}/ad-status`, `GET /decisions`, `GET /candidate`, `GET /rules`,
  `GET|POST /companies`, `GET|POST /contacts`, `POST /contacts/{id}/events`, `POST /packages/review`,
  `POST /reviews`, `POST /packages/tailoring`, `POST /tailoring`, `GET /resume?mode=…`,
  `GET /resume/versions/{id}`, `POST /skills/propose`, `GET /contacts?established=true|false`.

Ejemplo (PowerShell):
```
$h = @{ Authorization = "Bearer <token>"; "X-Agent-Name" = "mi-script" }
Invoke-RestMethod "http://127.0.0.1:8765/api/v1/jobs?mode=CANADA_VISA&q=snowflake" -Headers $h
```

### Guía completa para su IA local
Entregue a su agente o modelo local el archivo **`AGENT_GUIDE.md`** (en la carpeta de la app, en inglés): explica
las reglas, cómo conectarse (MCP, API REST, bandeja SQLite), las 23 herramientas, todas las rutas, los protocolos con
ejemplos, recetas de uso y un *system prompt* listo para copiar.

### Protocolos de respuesta (para quien programe un agente)
- **Revisión** `jsh-review/1`: `{"protocol_version","package_id","mode","provider","model","reviews":[{"job_id",
  "jd_hash","technical_fit_score" 0-100,"location_fit" yes|no|unknown,"work_authorization_fit"
  possible|blocked|unknown|requires_employer_confirmation,"recommendation"
  high_priority|review|low_priority|discard_suggested,"summary","evidence":[{"claim","jd_quote"}],"unknowns",
  "blockers","questions_for_employer","resume_adaptation_suggestions"}]}`. El paquete incluye el JSON Schema exacto.
- **CV adaptado** `jsh-tailoring/1`: `{"protocol_version","package_id","job_id","jd_hash","master_version_id",
  "changes":[{"change_id","action" rewrite|reorder|remove|add,"target_id" (S1, P1.H2, P1, skills),"proposed",
  "order","evidence_ids","rationale"}],"missing_requirements","questions_for_user"}`. Nunca puede cambiar
  puestos, empresas ni fechas.

---

## 6. Activos: decidir qué hacer con cada vacante

Cada tarjeta muestra el título, la empresa, la ubicación, la modalidad y unas etiquetas de color:

| Etiqueta | Significado |
|---|---|
| 🟢 **Elegible** | Cumple todas las reglas obligatorias. |
| 🟡 **Verificar** | Falta información importante (por ejemplo, el anuncio no dice si hay patrocinio o desde qué países se permite remoto). |
| 🟠 **Con alertas** | Una regla "suave" encontró algo a revisar. |
| 🔴 **Bloqueado** | No cumple una regla obligatoria ("dura"). No aparece en Activos salvo anulación manual o proceso en curso. |
| **Puntaje N** | Suma de las reglas de puntaje (por ejemplo, coincidencia de skills en la descripción). |
| **Skills CV N%** | Coincidencia de la JD con sus skills aprobados (sección 10.6). |
| **Agente N** | Puntaje que envió un agente externo (sección 5c). Opinión del agente, no suya. |
| **IA N** | Puntaje del análisis IA que usted **aceptó** (sección 5d). |

**Botones:**
- **Guardar:** la marca como interesante.
- **Tal vez / Revisar:** para decidir después.
- **Descartar:** pide un motivo (*No remoto desde país*, *Requiere autorización que no tengo*,
  *Skills insuficientes*, *Salario*, *Seniority*, *Empresa no confiable*, *Duplicado*,
  *Puesto expirado*, *No me interesa*, *Otro*) y una nota opcional. La vacante **se oculta pero
  no se borra**; puede restaurarla en cualquier momento.
- **Aplicar (página externa):** abre el sitio del empleador. Usted aplica allí.
- **Ver JD / detalle:** abre el detalle completo (ver secciones 7 y 7b: palabras clave, análisis IA,
  CV adaptado, empresa y contactos).
- **Ver empresa:** abre la ficha de la empresa (sección 9b).
- **Fijar:** la mantiene al principio de la lista (📌).

**Buscar:** escriba una palabra y presione Enter o **Buscar**. **Limpiar filtros** quita la
búsqueda. Si no aparece nada, la app explica por qué (búsqueda activa, vacantes bloqueadas o
descartadas).

Una vacante con aplicación **en proceso** (Aplicado, Filtro reclutador, Entrevistas, Oferta)
**siempre** aparece en Activos, aunque el anuncio haya expirado o una regla la bloquee.

---

### 6b. Etiquetas y vistas guardadas
- **Etiquetas:** en el detalle de una vacante, escriba etiquetas libres (p. ej. `Prioridad`, `Toronto`, `Referido`)
  y pulse Enter; luego **Guardar etiquetas**. En *Vacantes*, seleccione filas → **Etiquetas…** para agregar o quitar
  una etiqueta a varias a la vez. Se ven como chips en las tarjetas, se pueden buscar con la caja de búsqueda y usar
  en reglas (campo *Etiquetas (del usuario)*). Máximo 20 por vacante y 40 caracteres cada una. Solo usted las edita
  (los agentes las leen).
- **Filtros…** (en Activos y Vacantes): países, ubicación contiene, modalidad, fuente, puntaje mínimo, % de skills
  mínimo, **salario anual mínimo** (normalizado, sección 12b), aplicación (sin aplicar / aplicada / proceso activo),
  decisión, elegibilidad, etiquetas, "ocultar anuncios expirados o posiblemente cerrados" y "publicada en los
  últimos N días". **Aplicar** filtra sin guardar.
- **Vistas guardadas:** ponga un nombre (p. ej. *Canadá · Toronto · puntaje > 60 · sin aplicar*) y pulse **Guardar
  como vista nueva**. Elíjala en **Vista guardada**; la app la recuerda por modo. **Actualizar vista** guarda los
  cambios; el icono de papelera la elimina (las vacantes no cambian). **Quitar filtros** vuelve a ver todo.
  Los agentes pueden usar sus vistas: `GET /api/v1/jobs?saved_view=<nombre>` o la herramienta MCP
  `search_jobs(saved_view=…)`.

## 7. Detalle de una vacante

Se abre con **Ver JD / detalle**, haciendo clic en una fila de *Vacantes* o en el título de una
tarjeta del *Kanban*.

**Columna izquierda:**
- **¿Por qué?** Tabla con cada regla evaluada: ⛔ Bloqueo, ❓ Verificar, ❔ Desconocido,
  ⚠ Alerta, ➕ Puntaje, ✓ Cumple. Incluye la explicación y un fragmento del texto como evidencia.
- **JD vs CV (skills aprobados):** porcentaje de coincidencia y dos listas:
  - 🟢 skills que la vacante pide y usted **aprobó** (al pasar el mouse: el texto de la JD y la
    evidencia de su CV);
  - 🔴/🟡 requisitos que **no** están en sus skills aprobados (🟡 = aparece en su CV pero aún no
    lo aprueba; 🔴 = no aparece o usted lo rechazó).

  Los faltantes **nunca** se agregan a su perfil automáticamente.
- **Decisión:** la decisión actual y el botón **Restaurar** si está descartada.
- **Aplicación:** elija el estado (*Descubierto, Guardado, Preparando, Aplicado, Filtro
  reclutador, Entrevistas, Oferta, Rechazado, Retirado, Descartado*), la fecha del próximo
  seguimiento (AAAA-MM-DD) y una nota, y luego **Guardar estado**. Al marcar *Aplicado* se
  registra la fecha de hoy.
- **Anulación manual:** si no está de acuerdo con las reglas, elija *Incluir pese a reglas* o
  *Excluir* y escriba **el motivo (obligatorio)**.

**Columna derecha:**
- **Datos específicos del modo:**
  - *Canadá:* Empleador apoya permiso (unknown/yes/no/maybe), Evidencia CUSMA, Evidencia LMIA,
    Conversación de inmigración, Revisión NOC.
  - *México:* Tipo de contratación (unknown / nómina local / contratista internacional / EOR),
    Tipo de cambio (fuente y fecha), Tiempo estimado de contratación.
  - Al guardar, la vacante se vuelve a evaluar.
- **Notas:** escriba y presione **Guardar notas**.
- **Descripción (JD):** el texto guardado del anuncio.
- **Historial de decisiones:** todos los cambios de decisión con fecha.

> La app **no** determina elegibilidad legal ni migratoria. "Verificar" significa que debe
> confirmar con el empleador o con fuentes oficiales.

### 7b. Debajo del detalle: palabras clave, análisis IA, CV adaptado y empresa

- **Palabras clave de la JD y exportar para IA:** la app lee la JD y separa sus secciones
  (requisitos, deseables, responsabilidades, beneficios). Muestra las palabras clave como
  **Requeridas / Deseables / Mencionadas** (✓ verde = skill aprobado en su CV; pase el mouse para ver
  la frase de la JD), los años de experiencia pedidos, educación, modalidad, salario y frases sobre
  visa/patrocinio (⚠ si dice que *no* patrocina). Son pistas automáticas: verifíquelas.
  **Copiar JD para IA (Markdown)** copia la vacante en texto plano —hechos, palabras clave,
  coincidencia con su CV y la JD completa— lista para pegar en ChatGPT/Claude; también puede
  **Descargar .md** o **.json**.
- **Análisis IA:** **Paquete de revisión (esta vacante)** y **Pegar respuesta de la IA** (sección 5d).
  Cada análisis muestra puntaje, recomendación, ubicación, autorización de trabajo (nunca es una
  determinación legal), evidencia, dudas, preguntas para el empleador y sugerencias para el CV, con
  **Aceptar análisis** / **Rechazar**.
- **CV adaptado a esta vacante:**
  - **Propuesta automática (sin IA):** solo **reordena** su contenido aprobado (skills pedidos y
    logros con más palabras clave primero) y lista los requisitos que le faltan. No reescribe nada.
  - **Paquete para IA** / **Pegar propuesta de la IA:** la IA puede proponer reescrituras de su
    resumen y de sus logros, citando el elemento de su CV maestro en que se basa.
  - La revisión muestra cada cambio **Antes / Después**. La app marca en rojo lo que podría ser
    inventado: cifras nuevas, skills no aprobados, nombres de empresas o clientes, cargos, fechas o
    cantidades escritas en letras que no están en la evidencia, o texto sin evidencia. Esos cambios
    solo se aplican si usted marca **"Verifiqué que este texto es verdadero"**. Las reescrituras de la
    IA nunca vienen marcadas de antemano; los puestos, empresas y fechas no se pueden cambiar.
  - **Aprobar cambios seleccionados** crea una versión **adaptada** (con origen, modelo, paquete,
    plantilla y cambios aprobados/rechazados), la vincula a su aplicación y aparece **Descargar
    .docx** (en `resumes/tailored/`). El encabezado del documento (nombre, correo…) sale de
    *Skills y CV → Encabezado del CV generado*.
- **Empresa y contactos:** abre la empresa, lista sus contactos, permite elegir el **contacto de la
  aplicación** y crear uno nuevo.

---

## 8. Vacantes (tabla completa)

- Las columnas cambian según el modo (por ejemplo, *Patrocinio* y *Apoyo visa* en Canadá;
  *Remoto desde*, *Salario* y *Contratación* en México).
- Haga clic en el encabezado de una columna para ordenar.
- Use el selector **Todas / Activos / Bloqueadas / Descartadas** para filtrar.
- Haga clic en una fila para abrir el detalle.
- **Acciones en lote:** marque varias filas con las casillas y use **Guardar seleccionadas**,
  **Descartar seleccionadas** (pide motivo) o **Restaurar seleccionadas** (solo afecta a las
  descartadas). La app muestra la lista y pide **Confirmar** antes de cambiar nada.

### 8b. Eliminar vacantes definitivamente
- **Descartar** oculta una vacante y es reversible. **Eliminar** la borra de verdad.
- Desde el detalle (**Eliminar vacante…**) o en lote (**Vacantes → marque filas → Eliminar
  seleccionadas…**). Debe escribir **ELIMINAR** para confirmar.
- Se borran la vacante, su JD, evaluaciones, decisiones, aplicación e historial de estados, análisis
  IA y propuestas de CV. **Se conservan** los CV adaptados ya aprobados y las interacciones con
  contactos (quedan sin vínculo a la vacante).
- Antes se crea un backup automático (`…_pre_delete_jobs.db`): si se equivocó, restáurelo en
  *Configuración / Backups*.
- La app recuerda las vacantes eliminadas: **Descubrir, los agentes (bandeja, API, MCP), CSV y Excel no
  las vuelven a crear** (los agentes reciben "omitida"). Si usted la agrega a mano en *Agregar
  vacante*, vuelve a entrar. En *Historial → Vacantes eliminadas definitivamente* puede pulsar
  **Permitir reimportar**.
- Los agentes **no** pueden eliminar vacantes.

---

### 8c. ¿Sigue abierto el anuncio? (verificación de anuncios)
La app puede visitar la URL de cada vacante guardada y marcarla **⚠ Posiblemente cerrada** si la página responde
404/410, dice que la vacante ya no está disponible ("no longer available", "position has been filled", "la vacante
ha expirado"…) o redirige a una página genérica de empleos o de error del portal. Si la página no responde o bloquea
(403/429/5xx), queda **sin determinar** (nunca se marca por eso).

- **Nunca cambia ni borra nada por sí sola.** Usted decide: en el detalle → **Marcar como expirada** (cambia el
  estado del anuncio; reversible con *Marcar como activa otra vez*) o **Sigue abierta** (oculta el aviso). En
  *Inicio* hay una tarjeta con las posiblemente cerradas y un botón para marcarlas todas como expiradas.
- **Verificar ahora:** en el detalle (una vacante) o en *Inicio* (todas las pendientes).
- **Automática:** desactivada por defecto. En *Configuración → Verificación de anuncios* active la verificación
  automática, cada cuántas horas re-verificar (24 por defecto) y el máximo por ronda (25). Corre cada hora mientras
  la app está abierta. Sin interfaz: `JobSearchManager.exe --check-ads`.
- **Privacidad y seguridad:** solo se solicita la URL pública del anuncio (sin cookies ni datos suyos), con pausas
  entre solicitudes al mismo sitio; se bloquean direcciones locales o privadas. Algunos portales cargan el anuncio
  con JavaScript (p. ej. Ashby): ahí la página puede responder "abierta" aunque esté cerrada.

## 9. Kanban

Muestra una columna por estado de aplicación. Solo aparecen las vacantes que ya tienen un estado
(se asigna desde el detalle → *Aplicación*). Para mover una tarjeta:
- **arrástrela** a otra columna (la columna se ilumina al pasar), o
- cambie el **selector** de la tarjeta.

Cada cambio queda en el historial con la nota "Kanban".

### 9b. Empresas y Contactos
- **Empresas:** se crean solas al registrar vacantes. Haga clic en una fila para editar sitio web,
  página de empleos, ATS, **lista de seguimiento (★)**, **legitimidad** (sin verificar / verificada /
  sospechosa), evidencia y notas, y ver sus vacantes y contactos. Solo se puede eliminar una empresa
  sin vacantes.
- **Contactos:** recruiters y hiring managers. En el contacto puede **Registrar** interacciones
  (correo, llamada, mensaje, reunión, entrevista, nota) con fecha; la tabla muestra la última.
  Vincule un contacto a una aplicación desde el detalle de la vacante.
- **Contacto establecido** (sí/no): márquelo cuando ya hubo una conversación en ambos sentidos (no
  solo un mensaje enviado). La tabla tiene la columna *Establecido* y el filtro **Todos /
  Establecidos / Sin establecer**. Un agente puede marcarlo solo si usted nunca lo cambió.
- Los agentes pueden **crear** empresas y contactos y **completar campos vacíos**, pero nunca
  sobrescriben lo que usted escribió ni cambian su lista de seguimiento. Un contacto agregado por un
  agente indica su origen: verifique su identidad antes de confiar.

---

## 10. Skills y CV

Aquí la app aprende **qué sabe hacer usted**, a partir de **su** currículum. Regla de oro:
**todo lo que la app extrae es una propuesta**. Solo lo que usted aprueba cuenta para comparar
vacantes. La app nunca inventa skills, niveles, años, empleadores ni logros.

### 10.1 Perfil de candidato
- La primera vez, presione **Crear perfil** (por ejemplo "Data Engineer").
- Puede tener **varios perfiles** (por ejemplo uno técnico y otro no técnico) y usar uno distinto
  en cada modo: botones **Usar en CANADA_VISA** / **Usar en MEXICO_REMOTE**. La etiqueta verde
  muestra en qué modo está activo.

### 10.2 Importar su CV
- **Subir CV:** `.docx`, `.pdf` **con texto seleccionable** o `.txt`. También puede **pegar el
  texto**.
- Puede importar **varios CV** en el mismo perfil (por ejemplo, versión en inglés y en español):
  los skills repetidos suman evidencia; no se duplican.
- El archivo original se guarda **sin cambios** en `resumes/originals/`, con una huella SHA-256.
  *Ver texto extraído* muestra lo que la app leyó y confirma que el original está intacto.
- No compatibles: PDF escaneado (imagen), `.doc` antiguo (guárdelo como `.docx`) y archivos de
  más de 10 MB.

### 10.3 Revisar y aprobar skills
La tabla **Skills propuestos** muestra para cada skill:
- **Confianza:** *Alta* = aparece en su experiencia laboral; *Media* = aparece en su lista de
  skills o en el resumen; *Baja* = solo en el encabezado, o un término que la app no reconoce.
- **Evidencia:** el fragmento exacto de su CV. Haga clic en la fila para ver todas las evidencias
  y editar.
- **Nota:** aclaraciones de su CV, por ejemplo *"integration experience"* en
  "Databricks/PySpark (integration experience)". Respételas: indican el alcance real.

Botones: **Seleccionar confianza alta**, **Aprobar seleccionados**, **Rechazar seleccionados**.
Aprobar significa "tengo este skill y puedo demostrarlo".

**Llenado automático (opcional):** en *Importar CV* active **Aprobar automáticamente los skills de
confianza ALTA**. Desde ese momento, al importar un CV los skills encontrados en su experiencia laboral
se aprueban solos; los de confianza media/baja quedan como propuestas. Nunca se aprueba un skill que
usted rechazó. La columna **Origen** indica de dónde viene cada skill: *CV*, *Usted* o *Agente IA*
(sugerencias de un agente, que siempre quedan como propuestas).

Los skills no se eliminan: **Rechazar** o desactivar (*Activo*) los saca de la comparación con
vacantes, y queda el registro.

En **Skills aprobados** puede editar:
- **Alias:** otros nombres con que aparece en anuncios (por ejemplo "ADF" para Azure Data Factory).
- **Peso** (0.1–5): importancia en el porcentaje de coincidencia.
- **Etiqueta:** *Principal* o *Preferido*.
- **Nivel** y **Años:** solo si usted los confirma; la app nunca los calcula.
- **Activo:** desactívelo para ignorarlo temporalmente sin borrarlo.

**Agregar skill manualmente:** para skills reales que su CV no menciona. Quedan marcados como
"agregado por el usuario".

### 10.4 Datos del perfil
Puestos (título, empresa, fechas, logros), educación, certificaciones, idiomas y resumen se
proponen igual que los skills. Revise títulos y fechas con el lápiz ✏ antes de aprobar: la
lectura automática del formato puede equivocarse. Puede **Aprobar todos los datos propuestos**.

### 10.5 CV maestro
**Guardar versión maestra** crea una foto inmutable (v1, v2…) de sus skills y datos **aprobados**.
Las adaptaciones de CV por vacante (sección 7b) parten de la última versión maestra; si no existe,
la app la guarda automáticamente con lo aprobado.

### 10.5b Encabezado del CV generado
En **Encabezado del CV generado (datos personales)** escriba nombre, titular, correo, teléfono,
ubicación y enlaces. Solo se usan para crear sus `.docx` en esta computadora; **no** se envían en
los paquetes de IA, la API ni MCP (salvo que marque explícitamente *Incluir mis datos de
contacto*). La app también borra correos, teléfonos y enlaces de los fragmentos de su CV que salen
como evidencia. En *Configuración* puede elegir si los títulos del `.docx` van en inglés o español.

### 10.6 Cómo se calcula la coincidencia
- La app busca en la descripción de la vacante unos 220 skills conocidos (técnicos y no técnicos,
  en inglés y español), más sus skills propios y alias.
- **Coincidencia = skills pedidos que usted aprobó ÷ todos los skills reconocidos en la vacante.**
  Los skills generales de negocio (por ejemplo "stakeholders", "documentación") cuentan la mitad.
- Si usted no tiene skills aprobados, o la vacante no menciona skills reconocibles, la
  coincidencia es **desconocida (—)** y no afecta el puntaje: nunca se toma como 0 ni como 100.
- Columna **Skills CV** en *Vacantes* y etiqueta en cada tarjeta de *Activos*.

---

## 11. Reglas y Filtros

Las reglas deciden qué vacantes son elegibles, cuáles requieren verificación y su puntaje.
**No necesita programar** para crearlas ni cambiarlas.

### Reglas que vienen de inicio (puede editarlas o desactivarlas)
**Canadá:**
- *Puesto en Canadá* **o** *Remoto autorizado desde Canadá* (obligatorio; si falta el dato →
  Verificar).
- *Declaración de patrocinio ausente* → pide revisión ("Verificar").
- *Anuncio indica que no patrocina* (frases como "no sponsorship") → alerta.
- *Skills del CV: coincidencia alta (≥ 60%)* → +40; *coincidencia media (30–59%)* → +20.

**México:**
- *Trabajo remoto* (obligatorio).
- *Remoto permitido desde México* (MX, LATAM, AMERICAS o WORLDWIDE). Un anuncio remoto solo
  para EE.UU. queda **Bloqueado**.
- *Skills del CV: coincidencia alta (≥ 60%)* → +40; *coincidencia media (30–59%)* → +20.
- *Salario publicado* → +10.

> Las reglas de skills usan el campo **"Coincidencia con skills del CV (%)"**. También puede crear
> reglas con los campos *Skills del CV presentes en la JD* o *Skills de la JD que no tiene
> aprobados*; por ejemplo, excluir vacantes que piden "SAP" si usted no lo tiene. Si actualizó
> desde la versión anterior, la regla antigua *Skills principales en la JD* quedó desactivada
> (solo si usted nunca la había modificado).
>
> Con análisis IA **aceptados** (sección 5d) puede usar los campos *IA: ajuste técnico*,
> *IA: prioridad*, *IA: recomendación*, *IA: ubicación compatible* e *IA: autorización de trabajo*.
> Mientras no acepte un análisis, esos campos son *desconocidos*.

### Acciones sobre las reglas
- **Interruptor:** activa o desactiva la regla. Las vacantes se reevalúan al instante.
- **↑ ↓:** cambian el orden (prioridad).
- **✏ Editar** y **🗑 Eliminar:** al eliminar, el historial de versiones se conserva.

### Crear una regla (botón **Nueva regla**)
| Campo | Qué significa |
|---|---|
| Nombre | Cómo la verá en la lista. |
| Campo | Qué dato se revisa (título, país, descripción, salario, remoto desde…). |
| Operador | *es igual a*, *está en (lista)*, *contiene alguna palabra*, *≥*, *entre*, *tiene dato*, *no tiene dato*, *en los últimos N días*, etc. Solo aparecen los válidos para el campo elegido. |
| Valor | Texto, número, fecha AAAA-MM-DD o lista separada por comas (la pista bajo el formulario indica el formato). |
| Acción | **Requerir** (debe cumplirse), **Excluir si cumple**, **Marcar alerta**, **Sumar puntaje**, **Pedir revisión**. |
| Severidad | **Duro** bloquea; **Suave** solo alerta; **Informativo** no afecta. |
| Si el dato es desconocido | **Mantener** (no afecta), **Marcar para verificar** o **Excluir**. |
| Peso | Puntos que suma (solo para *Sumar puntaje*). |
| Grupo lógico + Operador de grupo | Para combinar reglas: varias reglas *Requerir* con el mismo grupo y **OR** se cumplen si se cumple cualquiera. |
| Explicación visible | Texto que verá en el detalle de la vacante. |

**Siempre use primero "Probar (simular)":** muestra cuántas vacantes cambiarían y cómo, **sin
guardar nada**. Si está conforme, presione **Guardar**. Cada cambio crea una nueva versión; las
anteriores se ven en *Versiones anteriores*.

**Ejemplo:** no quiero vacantes de Java.
Campo *Descripción del puesto (JD)* · Operador *contiene alguna palabra* · Valor `java` ·
Acción *Excluir si cumple* · Severidad *Duro* · Desconocido *Mantener* → Probar → Guardar.

### Perfiles de búsqueda
Un **perfil** es un conjunto de reglas con nombre. Cada modo tiene su *Perfil principal* y puede
tener más, por ejemplo "Solo senior", "Sin Java" o "Amplio".
- **Perfil de búsqueda activo** (arriba en *Reglas y Filtros*): elija qué perfil se usa. Las
  vacantes se reevalúan al instante. Sus decisiones, notas y aplicaciones **no cambian**.
- **Guardar como nuevo perfil:** copia las reglas del perfil activo a un perfil nuevo y lo
  activa. El perfil original queda intacto, así puede experimentar sin perder nada.
- **Renombrar** y **Eliminar perfil…** (el *Perfil principal* no se puede eliminar; al eliminar
  otro, elige cuál queda activo).
- El número **v** junto al nombre aumenta cada vez que cambia una regla del perfil.
- *Activos* muestra qué perfil está activo.

### Sugerencias basadas en sus descartes
Cuando descarta varias vacantes por el **mismo motivo** (3 o más), la app puede **sugerir** una
regla. Aparece un aviso 💡 en *Activos* y un recuadro amarillo en *Reglas y Filtros*.

| Motivo de descarte | Sugerencia |
|---|---|
| Empresa no confiable / No me interesa | Excluir las empresas descartadas 3+ veces. |
| No remoto desde país | Requerir que el anuncio permita remoto desde su país. |
| Seniority | Excluir los niveles descartados, o palabras del título como *junior*, *lead*, *director*. |
| Salario | Alertar si el salario máximo es ≤ al más alto que descartó (solo si todos los salarios usan la misma moneda). |
| Requiere autorización | Alertar si la JD menciona *security clearance*, *US citizen*, *green card*, etc. |
| No me interesa | Excluir palabras frecuentes en los títulos descartados. |

**Nada se aplica solo.** Una sugerencia nunca propone algo que aparezca en vacantes que usted
guardó, marcó *Tal vez* o tiene en proceso.
- **Revisar y crear regla:** abre el editor ya lleno. Pruebe con *Probar (simular)*, ajuste y
  presione **Guardar** solo si está de acuerdo.
- **Ignorar:** oculta esa sugerencia en ese modo.
- Si la regla ya existe en el perfil activo, la sugerencia desaparece.

---

## 11b. Informes: brechas de skills y salarios
Pestaña **Informes** (por modo; elija el alcance *Activos*, *Guardadas / en proceso* o *Todas (sin descartadas)*):

- **Brechas de skills:** los skills que piden las JD y que **no** están aprobados en su perfil activo, ordenados por
  puntaje (aparece en *requisitos* ×3, *deseables* ×2, en otra parte ×1), con cuántas vacantes los piden y su
  estado en su perfil: *no está en su perfil* (si tiene experiencia real, agréguelo en Skills y CV; si no, es
  candidato a aprender), *propuesto sin aprobar* (botón **Aprobar (lo tengo)**) o *rechazado / desactivado*.
  En "Detalle y acciones por skill" ve ejemplos de vacantes (clic para abrirlas). **Fortalezas** muestra sus skills
  aprobados más pedidos. **Copiar como Markdown** / **Descargar .md** para pegarlo en ChatGPT/Claude. Los agentes lo
  leen con `GET /api/v1/insights/skill-gaps` o la herramienta MCP `get_skill_gaps`.
- **Salarios normalizados:** cuántas vacantes tienen salario comparable, mediana, mínimo y máximo anual en su moneda
  base, las 10 mejor pagadas y los tipos de cambio usados.

## 12. Excel

La base de datos de la app es la fuente de verdad; el Excel es una **vista editable**. No necesita
tener Excel instalado para usar la app.

### Exportar
Pestaña **Excel / Importar → Exportar libro Excel**. Se guarda en la carpeta `excel\`:
- `Job_Search_Canada.xlsx` (modo Canadá) o `Job_Search_Mexico.xlsx` (modo México).

Hojas comunes: **Dashboard**, **Activos**, **Vacantes**, **Empresas**, **Contactos**, **Filtros**
(reglas del perfil activo), **Ubicaciones**, **Salarios**, **Fuentes**, **Estados** (valores
permitidos), **Historial** y **Configuracion**. **Skills** lista los skills del perfil de candidato
activo del modo (editable: estado, activo, etiqueta, peso, alias, nivel, años, nota) y
**CV_Versiones** las versiones maestras y adaptadas. **Prompts** (plantillas de instrucciones) y
**Analisis_IA** (análisis recibidos/aceptados del modo) son solo de lectura: no se reimportan.

Hojas propias de cada modo (los datos del modo, en verde, se editan aquí):

| Canadá | México |
|---|---|
| **Visa_WorkPermit**: apoyo del empleador, conversación de inmigración | **Trabajo_Remoto**: tiempo estimado de contratación |
| **CUSMA_LMIA_Evidencias**: evidencia CUSMA, LMIA y revisión NOC | **Contratacion_Internacional**: tipo de contratación |
| **Empleadores_Permisos**: resumen por empresa (solo lectura) | **Ingresos_Prioridad**: salario y nota de tipo de cambio |

El libro de México nunca incluye columnas de visa canadiense.

### Editar
- **Solo las columnas verdes** se importan: `user_decision`, `discard_reason`,
  `application_status`, `date_applied`, `next_followup`, `notes`, los datos del modo y, en
  *Filtros*, nombre, activa, valor, acción, severidad, peso, prioridad y explicación.
- Use las listas desplegables. Los valores son **códigos** (por ejemplo `discarded`, `salary`,
  `applied`); la hoja *Estados* muestra su significado en español.
- Fechas en formato **AAAA-MM-DD**.
- **No cambie** `job_id`, `rule_id` ni `row_version`.
- Puede agregar una vacante nueva en una fila sin `job_id` (con al menos `job_title`).

### Reimportar
1. Asegúrese de estar en el **mismo modo** con que exportó (si no, el archivo se rechaza). Si
   exportó con otro **perfil** activo, los cambios de la hoja *Filtros* se ignoran (con aviso);
   el resto sí se importa.
2. **Subir .xlsx**. Verá una **vista previa** con cada cambio (valor actual → valor del Excel).
3. **Conflictos:** si una vacante cambió en la app después de exportar, sus cambios se marcan con
   ⚠ y **no se aplican**, salvo que marque *Aplicar también conflictos*.
4. **Advertencias:** valores inválidos (por ejemplo, un estado mal escrito) se ignoran y se
   listan.
5. Presione **Aplicar cambios**. Antes de aplicar se crea **un backup automático**.

---

### 12b. Salarios: una sola moneda y por año
Cada salario se convierte a un monto **anual** en su **moneda base** (CAD por defecto) para comparar, filtrar y usar
en reglas (campos *Salario anual mínimo/máximo normalizado*). En las tarjetas se ve, por ejemplo,
`≈ 140k–168k CAD/año (USD @1.4, 2026-10-30, BCE)`.

- **Periodo:** año, mes (×12), quincena (×26), semana (×52), día (× días/año, 260) y hora (× horas/año, 2080).
  Si el periodo es desconocido, **no** se normaliza (no se adivina).
- **Moneda:** la de la vacante; si está vacía se **supone** por el país del puesto (CA→CAD, MX→MXN, US→USD…) y se
  indica "moneda supuesta".
- **Tipos de cambio:** *Configuración → Moneda y tipos de cambio*. **Actualizar (BCE)** descarga las referencias
  diarias del Banco Central Europeo (frankfurter.dev, gratis; solo se envían los códigos de moneda). Se guardan con su
  fecha y fuente. Puede fijar un tipo **manual** (1 moneda = ? base); las actualizaciones automáticas nunca lo
  sobrescriben. Con "Actualizar automáticamente" activo, se refrescan solos si tienen más de 7 días y hay salarios
  en otras monedas. Ahí también cambia la moneda base y las horas/días por año (las vacantes se re-evalúan).
- El libro Excel (hoja *Salarios* y columnas de vacantes) incluye el salario anual normalizado y la moneda base.

## 13. Importar un CSV (por ejemplo, su lista anterior)

1. **Excel / Importar → Subir .csv**. Se aceptan separadores `,` `;` o tabulador y acentos
   (UTF-8 o Windows).
2. La app **sugiere** a qué campo corresponde cada columna. **Revíselo**: para cada columna
   elija el campo correcto, *(ignorar)* o *(guardar como metadato)*. Las columnas no reconocidas
   se guardan como metadato, no se pierden. Debe haber una columna asignada a **Título**.
3. **Vista previa:** muestra las filas *nuevas*, las que *ya existen* (se actualizan sin tocar
   sus notas ni decisiones), los *duplicados en el archivo* y los *errores* con el motivo.
4. **Importar** (crea un backup antes). La app recuerda el mapeo para la próxima vez.

Importar el mismo archivo dos veces **no** crea duplicados.

**Codificación (acentos):** la app detecta UTF-8 y Windows automáticamente. Si en los ejemplos
del mapeo ve acentos raros (por ejemplo `Ã©` o `√©`), cambie el selector **Codificación**:
- **Mac Roman:** archivos guardados como "CSV" clásico desde **Excel para Mac**.
- **UTF-8:** "CSV UTF-8" de Excel, Google Sheets o Numbers.
- **Windows:** "CSV" desde Excel para Windows.

Consejo: en Excel (Windows o Mac) guarde como **"CSV UTF-8 (delimitado por comas)"** para evitar
problemas de acentos.

---

## 14. Historial, backups y cambiar de computadora

- **Historial:** lista de descartadas con botón **Restaurar** (vuelve a la decisión previa al
  descarte) y la bitácora de todo cambio: decisiones, estados, reglas e importaciones.
- **Backups:** en *Configuración / Backups → Crear backup ahora*. También se crean
  automáticamente antes de cada importación y de cada actualización de la base de datos.
  Están en `backups\` como archivos `.db`. La app conserva los **40 backups automáticos** más
  recientes; los manuales y los *pre_restore* nunca se borran.
- **Restaurar un backup:** en *Configuración / Backups*, junto a cada backup, presione
  **Restaurar…**. La app revisa que el archivo sea válido y muestra cuántas vacantes contiene.
  Al confirmar:
  1. crea una copia del estado actual (`…_pre_restore.db`), así puede deshacerlo restaurando esa
     copia;
  2. reemplaza los datos por los del backup;
  3. recarga la página.

  Los cambios hechos después de ese backup se pierden, salvo en la copia *pre_restore*.
- **Cambiar de computadora (mismo sistema):** cierre la app y copie la carpeta completa (o el
  `.zip` en Mac). Todo viaja con ella.

### Usar los mismos datos en Windows y en Mac
Los programas son distintos para cada sistema, pero **los datos son compatibles**: la misma
base de datos, Excel y backups funcionan en ambos.

**Opción A — mover sus datos de una computadora a otra:**
1. Cierre la app en la computadora de origen.
2. Copie las carpetas `data`, `excel` y `backups` (y `resumes` cuando exista) de la carpeta de la
   app de origen a la carpeta de la app del otro sistema, reemplazando las que estén vacías.
3. Abra la app en la otra computadora.

**Opción B — una sola carpeta de datos compartida (por ejemplo, en una memoria USB):**
```
Memoria USB/
  JobSearchManager-Windows/      ← app para Windows (incluye jsh_home.txt)
  JobSearchManager-macOS/        ← app para Mac (incluye jsh_home.txt)
  JobSearchData/                 ← SUS DATOS, compartidos por ambas
```
En cada carpeta de la app cree un archivo de texto llamado **`jsh_home.txt`** con esta línea:
```
../JobSearchData
```
Use `/` (no `\`) para que funcione en ambos sistemas. Al abrir la app, Windows o Mac usarán esa
misma carpeta de datos. La ruta actual se ve en *Configuración / Backups*.

> ⚠ No abra la app en dos computadoras **a la vez** sobre la misma carpeta de datos.
> ⚠ En Mac, guarde la **app** en el disco de la Mac (no en la USB FAT32/exFAT); en la USB deje
> solo `JobSearchData` y, si quiere, el `.zip` de cada app.

### Carpetas
```
JobSearchManager/
  JobSearchManager.exe              ← Windows: doble clic para abrir
  Iniciar JobSearchManager.command  ← macOS: doble clic para abrir
  JobSearchManager                  ← macOS: programa (lo abre el .command)
  _internal/                        ← componentes del programa (no tocar)
  data/                             ← base de datos (jobsearch.db) — privada
  excel/                            ← libros Excel exportados
  backups/                          ← copias de seguridad
  logs/                             ← registro técnico (sin contraseñas)
  resumes/originals/                ← copias intactas de los CV importados
  resumes/tailored/                 ← CV adaptados (.docx) que usted aprobó
  exports/                          ← paquetes para IA (.md / .json)
  config/                           ← reservada
  source/                           ← código fuente para reconstruir
  jsh_home.txt                      ← (opcional) carpeta de datos compartida
  INSTRUCCIONES.md                  ← esta guía
```

---

### 14b. Borrar todos los datos
*Configuración → Zona peligrosa → Borrar todos los datos…*. Escriba **BORRAR TODO** (en la interfaz en inglés:
**DELETE ALL**) para confirmar.

- Se borran: vacantes, JD, decisiones, aplicaciones, empresas, contactos, perfiles de candidato, CV y skills,
  reglas personalizadas, vistas, etiquetas, tipos de cambio, análisis IA, historial, archivos de `resumes\`,
  `exports\` y `excel\`, los backups y los logs.
- Quedan: los modos, las reglas y perfiles **por defecto**, las plantillas de instrucciones y sus preferencias de
  apariencia/idioma. Los **tokens de la API se regeneran** (actualice sus agentes).
- **Conservar UNA copia final** (activado por defecto): antes de borrar se guarda `backups\…_pre_wipe.db`; puede
  restaurarla desde *Configuración → Restaurar…*. Si la desactiva, no hay forma de recuperar nada.
- Para empezar de cero sin la app: cierre la app y borre la carpeta `data\` (y, si quiere, `resumes\`, `excel\`,
  `exports\`, `backups\`, `logs\`); al abrirla se crea una base nueva.

## 15. Problemas comunes

| Problema | Solución |
|---|---|
| El navegador no se abre | Abra manualmente `http://127.0.0.1:8765` (o el puerto que indica la ventana negra o de Terminal). |
| Aviso de SmartScreen o antivirus (Windows) | Es un ejecutable sin firma. *Más información → Ejecutar de todas formas*, o pida a TI que lo autorice. |
| "Activos" está vacío | Lea la explicación en pantalla: puede haber una búsqueda activa, vacantes bloqueadas por reglas (*Vacantes → Bloqueadas*) o descartadas (*Historial*). |
| Una vacante que me interesa aparece Bloqueada | Abra el detalle, vea qué regla la bloquea y ajuste la regla o use una **anulación manual** con motivo. |
| El Excel reimportado no aplicó un cambio | Revise las advertencias y conflictos de la vista previa. Exporte de nuevo, edite y reimporte. |
| Errores de archivo bloqueado | Mueva la carpeta fuera de OneDrive/iCloud Drive o pause la sincronización. |
| **Mac:** "no se puede abrir porque proviene de un desarrollador no identificado" | Ver sección 2 → macOS (*Abrir de todas formas* en Privacidad y seguridad, o clic derecho → Abrir). |
| **Mac:** "Permiso denegado" o no pasa nada al abrir el `.command` | En Terminal: `chmod +x ~/Documents/JobSearchManager/*.command ~/Documents/JobSearchManager/JobSearchManager` |
| **Mac:** la app dice que corre desde una ubicación de solo lectura | Mueva la carpeta a *Documentos* y ejecute `xattr -dr com.apple.quarantine ~/Documents/JobSearchManager`. |
| **Mac:** "Bad CPU type in executable" | Descargó la versión de otra arquitectura: use `arm64` para Apple Silicon y `x86_64` para Intel. |
| No puede escribir en la carpeta de la app | La app lo indica al iniciar. Cópiela a una carpeta con permisos de escritura o use `--home`. |
| Una búsqueda de *Descubrir* da error | Lea el detalle en el *Historial de ejecuciones*. 404 = identificador del tablero incorrecto; 403 = la fuente no permite acceso automático (use *Agregar vacante*); sin internet = reintente más tarde. Sus vacantes no se modifican. |
| Algo falló | Revise `logs\jsh.log` y comparta ese archivo (no contiene contraseñas). |
| El agente externo recibe "database is locked" | Use `timeout=30` (o `PRAGMA busy_timeout=30000`) y haga `COMMIT` después de cada vacante. No abra la base por red. |
| Las vacantes del agente no aparecen | Presione **⟳**. Si siguen sin aparecer, vea *Configuración / Backups → filas rechazadas* (motivo exacto) o ejecute `--sync`. |
| La API responde 401 / 403 / 503 | 401 = token ausente o regenerado (cópielo de nuevo en *Configuración*); 403 = token de solo lectura intentando escribir, o la solicitud no viene de `127.0.0.1`; 503 = API desactivada. |
| El agente MCP no ve la herramienta / no conecta | Copie de nuevo el bloque de *Configurar un cliente MCP* (la ruta del `.exe` cambia si movió la carpeta) y reinicie el cliente (Claude Desktop, VS Code…). |
| "No se importó: jd_hash no coincide" | La IA no copió el hash exacto del paquete. Pídale que repita la respuesta copiando `job_id` y `jd_hash` tal cual, o genere un paquete nuevo. |
| Un cambio del CV adaptado sale en rojo | La app detectó algo que no está en su CV (cifra, nombre, cargo, skill). Solo márquelo como verificado si es verdad; si no, déjelo sin seleccionar. |

---

## 16. Privacidad y seguridad

- La app solo escucha en `127.0.0.1`: otras computadoras de la red no pueden verla.
- Solo usa internet cuando usted ejecuta una búsqueda de *Descubrir* (envía únicamente las palabras clave y los
  países de la búsqueda), cuando actualiza los **tipos de cambio** (solo códigos de moneda, a frankfurter.dev; también
  automático si los tipos tienen más de 7 días, desactivable) y cuando **verifica anuncios** (solo la URL pública
  de cada anuncio; automático solo si usted lo activa). **Nunca** envía su CV ni sus datos. No llama a ninguna IA.
- Los paquetes de IA solo salen de su computadora si **usted** los pega en un chat o se los da a un
  agente. Contienen su perfil **aprobado** (skills, experiencia, educación), sin nombre, correo,
  teléfono ni enlaces salvo que marque *Incluir mis datos de contacto*. Revise el `.md` antes de
  pegarlo si tiene dudas.
- La API local exige token y solo acepta solicitudes de esta computadora. Trate los tokens como
  contraseñas; regenérelos si los compartió por error. MCP con `--read-only` impide escrituras.
- El texto de los anuncios se trata como no confiable: nunca se ejecuta y se muestra como texto.
- Las reglas no ejecutan código; solo comparan datos.
- Un agente externo usa internet y sus datos según **su propia** configuración: la app no lo
  controla. Lo que el agente escribe se trata como texto no confiable, igual que los anuncios.
- No se guardan contraseñas ni claves en la app ni en el Excel.
