1. Preparar el Entorno
Abrir la terminal, crear una carpeta para el proyecto y configurar el entorno virtual:

# Crear la carpeta y entrar en ella
mkdir extractor-runt
cd extractor-runt

# Crear el entorno virtual
python -m venv venv

# Activar el entorno virtual (Windows)
venv\Scripts\activate
# (Si usas Linux/Mac: source venv/bin/activate)

# Instalar las librerías necesarias
pip install fastapi uvicorn pdfplumber python-multipart supabase pandas openpyxl

- FastAPI: Framework web de alto rendimiento para construir los endpoints de la API en Python.

- Uvicorn: Servidor web ASGI que ejecuta la aplicación de FastAPI y maneja las peticiones entrantes de forma asíncrona.

- pdfplumber: Herramienta de extracción de datos que lee documentos PDF y captura texto plano junto con las coordenadas precisas para estructurar tablas.

- python-multipart: Dependencia requerida por FastAPI para procesar datos en formato multipart/form-data, permitiendo la recepción de archivos adjuntos.

- supabase: Cliente oficial para conectar el backend con la base de datos PostgreSQL alojada en Supabase y ejecutar consultas e inserciones.

- pandas: Librería de análisis de datos utilizada para procesar los registros de la base de datos y aplanarlos en una estructura tabular (DataFrame).

- openpyxl: Motor de escritura que permite a pandas exportar los DataFrames directamente al formato estándar de Microsoft Excel (.xlsx).

2. Conseguir las credenciales de Supabase
Mientras se instalan las dependencias, ve a tu panel de Supabase:

- En el menú lateral izquierdo, haz clic en el ícono del engranaje (Project Settings).

- Ve a la sección API.

- Copia la URL que aparece en Project URL.

- Copia la clave pública que aparece en Project API keys (la que tiene la etiqueta anon public).
- Guarda estos dos datos, los usaremos más adelante.

Comando para ejecutar el servidor
- uvicorn main:app --reload

# 1. Crear el proyecto con Vite y React
npm create vite@latest frontend-runt -- --template react

# 2. Entrar a la carpeta
cd frontend-runt

# 3. Instalar dependencias base
npm install
npm install @supabase/supabase-js axios

# 4. Instalar y configurar Tailwind CSS
npm install -D tailwindcss postcss autoprefixer
npx tailwindcss init -p


1. Activar CORS en tu Backend (Python)
Abre tu archivo main.py y agrega estas líneas justo debajo de donde creas la variable app = FastAPI(...):

Python
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="API Extracción RUNT con IA")

# --- NUEVO: Configuración CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"], # Permite tu frontend local
    allow_credentials=True,
    allow_methods=["*"], # Permite POST, GET, etc.
    allow_headers=["*"],
)
# ---------------------------------

# (Aquí sigue tu código normal: SUPABASE_URL = ...)