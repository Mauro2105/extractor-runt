import gc
import io
import json
import os
import time
import google.generativeai as genai
import pandas as pd
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from supabase import Client, create_client
from pypdf import PdfReader, PdfWriter

load_dotenv()

app = FastAPI(title="API Extracción RUNT")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# Configuración de Credenciales
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Faltan las credenciales de Supabase en el archivo .env")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

gemini_key = os.getenv("GEMINI_API_KEY")
if not gemini_key:
    raise RuntimeError("Falta la credencial de Gemini en el archivo .env")

genai.configure(api_key=gemini_key)
security = HTTPBearer()

# Umbral de páginas: <= 15 procesa en línea (sale en el Excel de inmediato); > 15 va a segundo plano
UMBRAL_PAGINAS_BACKGROUND = 15


def verificar_usuario(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    try:
        respuesta = supabase.auth.get_user(token)
        return respuesta.user
    except Exception:
        raise HTTPException(
            status_code=401,
            detail="Acceso denegado. Token inválido, falso o expirado."
        )


def procesar_documento_por_lotes(pdf_bytes: bytes, filename: str) -> list[str]:
    """
    Fracciona el PDF en lotes de 5 páginas, consulta a Gemini, guarda en Supabase
    y libera memoria RAM en cada iteración.
    """
    nombres_procesados = []

    try:
        print(f"[*] Iniciando procesamiento para: {filename}")

        reader = PdfReader(io.BytesIO(pdf_bytes))
        total_pages = len(reader.pages)
        chunk_size = 5

        prompt = """
        Analiza este documento del RUNT que puede contener múltiples pantallazos o páginas.
        Extrae la información de todas las personas que aparezcan y consolida todos sus registros.
        Devuelve ÚNICAMENTE un JSON válido con esta estructura exacta, sin texto adicional:
        {
          "personas": [
            {
              "datos_basicos": {
                "tipo_documento": "string",
                "numero_documento": "string",
                "nombres": "string",
                "apellidos": "string",
                "estado_runt": "string",
                "celular": "string",
                "correo_electronico": "string"
              },
              "registros_ubicabilidad": [
                {
                  "direccion": "string",
                  "municipio_departamento": "string",
                  "telefono": "string",
                  "tipo_direccion": "string",
                  "estado_direccion": "string"
                }
              ]
            }
          ]
        }
        Si un campo está vacío o dice "SIN REGISTRO", usa el valor null de JSON (sin comillas).
        """

        for i in range(0, total_pages, chunk_size):
            rango_inicio = i + 1
            rango_fin = min(i + chunk_size, total_pages)
            print(f"[*] Procesando páginas {rango_inicio} a {rango_fin} de {total_pages}...")

            writer = PdfWriter()
            for j in range(i, rango_fin):
                writer.add_page(reader.pages[j])

            chunk_bytes_io = io.BytesIO()
            writer.write(chunk_bytes_io)
            chunk_bytes_io.seek(0)

            pdf_part = {
                "mime_type": "application/pdf",
                "data": chunk_bytes_io.read()
            }

            max_intentos = 3
            response = None

            for intento in range(max_intentos):
                try:
                    model = genai.GenerativeModel('gemini-3.5-flash-lite')
                    response = model.generate_content([pdf_part, prompt])
                    break
                except Exception as e:
                    if intento == max_intentos - 1:
                        print(f"[-] Fallo procesando lote {rango_inicio}-{rango_fin}. Error: {e}")
                    time.sleep(2)

            # Liberar memoria del lote actual apenas termina la llamada
            del pdf_part, chunk_bytes_io, writer
            gc.collect()

            if not response:
                continue

            try:
                texto_limpio = response.text.strip().removeprefix("```json").removesuffix("```").strip()
                datos_json = json.loads(texto_limpio)
            except Exception as e:
                print(f"[-] Error parseando JSON en páginas {rango_inicio}-{rango_fin}: {e}")
                continue

            personas_extraidas = datos_json.get("personas", [])

            for persona in personas_extraidas:
                datos_basicos = persona.get("datos_basicos", {})
                registros = persona.get("registros_ubicabilidad", [])

                if not datos_basicos or not datos_basicos.get("numero_documento"):
                    continue

                persona_query = supabase.table("personas").select("id").eq("numero_documento", datos_basicos["numero_documento"]).execute()
                if len(persona_query.data) > 0:
                    persona_id = persona_query.data[0]["id"]
                else:
                    insert_persona = supabase.table("personas").insert(datos_basicos).execute()
                    persona_id = insert_persona.data[0]["id"]

                query_existentes = supabase.table("registros_ubicabilidad").select("direccion, municipio_departamento, telefono").eq("persona_id", persona_id).execute()
                direcciones_existentes = query_existentes.data

                for registro in registros:
                    es_duplicado = False
                    for ext in direcciones_existentes:
                        if (ext.get("direccion") == registro.get("direccion") and
                            ext.get("municipio_departamento") == registro.get("municipio_departamento") and
                            ext.get("telefono") == registro.get("telefono")):
                            es_duplicado = True
                            break

                    if not es_duplicado:
                        registro["persona_id"] = persona_id
                        supabase.table("registros_ubicabilidad").insert(registro).execute()
                        direcciones_existentes.append(registro)

                nombre = datos_basicos.get("nombres")
                nombre_seguro = str(nombre) if nombre is not None and str(nombre).strip() != "" else "Desconocido"
                nombres_procesados.append(nombre_seguro)
                print(f"[+] Registrado/Actualizado: {nombre_seguro} ({datos_basicos['numero_documento']})")

            # Solo pausar entre lotes si aún quedan páginas pendientes
            if rango_fin < total_pages:
                time.sleep(4)

        print(f"[OK] Procesamiento completo para: {filename}. Total personas: {len(nombres_procesados)}")
        return nombres_procesados

    except Exception as e:
        print(f"[!] Error crítico procesando {filename}: {str(e)}")
        raise


@app.api_route("/", methods=["GET", "HEAD"])
def read_root():
    return {"status": "ok", "message": "API Extractor RUNT activa y operando"}


@app.post("/procesar-pdf/")
def procesar_pdf(background_tasks: BackgroundTasks, file: UploadFile = File(...), usuario=Depends(verificar_usuario)):
    if not file.filename.lower().endswith(".pdf") or file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="El archivo debe ser un documento PDF válido.")

    pdf_bytes = file.file.read()

    # Contamos las páginas para decidir si corre en línea o en segundo plano
    try:
        total_pages = len(PdfReader(io.BytesIO(pdf_bytes)).pages)
    except Exception:
        raise HTTPException(status_code=400, detail="No se pudo leer el archivo PDF. Verifica que no esté corrupto.")

    if total_pages > UMBRAL_PAGINAS_BACKGROUND:
        background_tasks.add_task(procesar_documento_por_lotes, pdf_bytes, file.filename)
        return {
            "status": "success",
            "message": f"Documento grande ({total_pages} páginas) recibido. Se está procesando en segundo plano; usa 'Exportar Consolidado' en unos minutos."
        }

    # Documentos normales (<= 15 páginas): procesa antes de responder para que la descarga incluya los datos nuevos
    try:
        nombres_procesados = procesar_documento_por_lotes(pdf_bytes, file.filename)
        if not nombres_procesados:
            raise HTTPException(status_code=400, detail="No se encontró información de personas en el documento.")

        return {
            "status": "success",
            "message": f"Datos procesados correctamente ({len(nombres_procesados)} registros)."
        }
    except HTTPException:
        raise
    except Exception as e:
        error_str = str(e)
        if "429" in error_str or "Quota exceeded" in error_str:
            raise HTTPException(
                status_code=429,
                detail="Límite de cuota alcanzado. Espera un minuto antes del siguiente documento."
            )
        raise HTTPException(status_code=500, detail=error_str)


@app.get("/descargar-excel/")
def descargar_excel(usuario=Depends(verificar_usuario)):
    # --- PAGINACIÓN COMPLETA PARA SUPERAR EL LÍMITE DE 1,000 FILAS DE SUPABASE ---
    datos = []
    inicio = 0
    tamano_lote = 1000

    while True:
        response = supabase.table("personas").select(
            "id, created_at, numero_documento, nombres, apellidos, celular, correo_electronico, registros_ubicabilidad(fecha_carga, direccion, municipio_departamento, telefono, tipo_direccion, estado_direccion)"
        ).order("id").range(inicio, inicio + tamano_lote - 1).execute()

        lote = response.data
        if not lote:
            break

        datos.extend(lote)
        if len(lote) < tamano_lote:
            break

        inicio += tamano_lote

    if not datos:
        raise HTTPException(status_code=404, detail="No hay datos en la base de datos.")

    filas_excel = []
    for fila in datos:
        registros = fila.get("registros_ubicabilidad", [])
        fecha_creacion_persona = fila.get("created_at")[:10] if fila.get("created_at") else ""
        documento = str(fila.get("numero_documento")).strip() if fila.get("numero_documento") is not None else ""

        if not documento:
            continue

        if not registros:
            filas_excel.append({
                "Fecha Carga": fecha_creacion_persona,
                "Documento": documento,
                "Nombres": fila.get("nombres"),
                "Apellidos": fila.get("apellidos"),
                "Celular": fila.get("celular"),
                "Correo": fila.get("correo_electronico"),
                "Dirección": None,
                "Municipio": None,
                "Teléfono": None,
                "Tipo Dirección": None,
                "Estado Dirección": None
            })
        else:
            for reg in registros:
                filas_excel.append({
                    "Fecha Carga": reg.get("fecha_carga")[:10] if reg.get("fecha_carga") else fecha_creacion_persona,
                    "Documento": documento,
                    "Nombres": fila.get("nombres"),
                    "Apellidos": fila.get("apellidos"),
                    "Celular": fila.get("celular"),
                    "Correo": fila.get("correo_electronico"),
                    "Dirección": reg.get("direccion"),
                    "Municipio": reg.get("municipio_departamento"),
                    "Teléfono": reg.get("telefono"),
                    "Tipo Dirección": reg.get("tipo_direccion"),
                    "Estado Dirección": reg.get("estado_direccion")
                })

    if not filas_excel:
        raise HTTPException(status_code=404, detail="No hay registros válidos para exportar.")

    df = pd.DataFrame(filas_excel)
    df = df.sort_values(by=['Documento', 'Fecha Carga'])
    df['Indice_Dir'] = df.groupby('Documento').cumcount() + 1

    datos_basicos = df[['Documento', 'Nombres', 'Apellidos', 'Celular', 'Correo', 'Fecha Carga']].copy()
    datos_basicos = datos_basicos.sort_values('Fecha Carga', ascending=False).drop_duplicates('Documento')
    datos_basicos = datos_basicos.rename(columns={'Fecha Carga': 'Última Fecha de Carga'})

    columnas_variables = ['Dirección', 'Municipio', 'Teléfono', 'Tipo Dirección', 'Estado Dirección']
    df_pivot = df.pivot(index='Documento', columns='Indice_Dir', values=columnas_variables)
    df_pivot.columns = [f"{columna} {numero}" for columna, numero in df_pivot.columns]
    df_pivot = df_pivot.reset_index()

    df_final = pd.merge(datos_basicos, df_pivot, on='Documento')

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df_final.to_excel(writer, index=False, sheet_name="Data RUNT")

    output.seek(0)
    headers = {
        'Content-Disposition': 'attachment; filename="historial_runt.xlsx"'
    }
    return Response(
        content=output.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers
    )