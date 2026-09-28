import io
import json
import os
import time

import google.generativeai as genai
import pandas as pd
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from supabase import Client, create_client

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
# Extraer de variables de entorno
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


def verificar_usuario(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    try:
        # Supabase valida matemáticamente el token y verifica que la sesión esté activa
        respuesta = supabase.auth.get_user(token)
        return respuesta.user
    except Exception:
        raise HTTPException(
            status_code=401,
            detail="Acceso denegado. Token inválido, falso o expirado."
        )


@app.get("/")
def read_root():
    return {"status": "ok", "message": "API Extractor RUNT activa y operando"}


@app.post("/procesar-pdf/")
def procesar_pdf(file: UploadFile = File(...), usuario=Depends(verificar_usuario)):
    if not file.filename.lower().endswith(".pdf") or file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="El archivo debe ser un documento PDF válido.")

    try:
        # 1. Leemos el PDF directamente en la memoria RAM
        pdf_bytes = file.file.read()

        # 2. Empaquetamos el archivo para enviarlo en línea
        pdf_part = {
            "mime_type": "application/pdf",
            "data": pdf_bytes
        }

        max_intentos = 3
        response = None

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

        for intento in range(max_intentos):
            try:
                # Usamos el modelo optimizado para velocidad
                model = genai.GenerativeModel('gemini-3.5-flash-lite')

                # 3. Enviamos el prompt y el archivo directamente en un solo viaje de red
                response = model.generate_content([pdf_part, prompt])
                break
            except Exception as e:
                if intento == max_intentos - 1:
                    raise Exception(f"Fallo tras {max_intentos} intentos. {str(e)}")
                time.sleep(2)

        if not response:
            raise Exception("No se recibió respuesta del modelo IA.")

        texto_limpio = response.text.strip().removeprefix("```json").removesuffix("```").strip()
        datos_json = json.loads(texto_limpio)

        personas_extraidas = datos_json.get("personas", [])
        if not personas_extraidas:
            raise HTTPException(status_code=400, detail="No se encontró información de personas en el documento.")

        nombres_procesados = []

        for persona in personas_extraidas:
            datos_basicos = persona.get("datos_basicos", {})
            registros = persona.get("registros_ubicabilidad", [])

            # Saltar si este registro en particular no tiene cédula
            if not datos_basicos or not datos_basicos.get("numero_documento"):
                continue

            # 1. Guardar o recuperar Persona en la base de datos
            persona_query = supabase.table("personas").select("id").eq("numero_documento", datos_basicos["numero_documento"]).execute()
            if len(persona_query.data) > 0:
                persona_id = persona_query.data[0]["id"]
            else:
                insert_persona = supabase.table("personas").insert(datos_basicos).execute()
                persona_id = insert_persona.data[0]["id"]

            # 2. Extraer Direcciones Existentes para Idempotencia
            query_existentes = supabase.table("registros_ubicabilidad").select("direccion, municipio_departamento, telefono").eq("persona_id", persona_id).execute()
            direcciones_existentes = query_existentes.data

            # 3. Guardar solo direcciones nuevas que no estén en la BD
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

            nombres_procesados.append(datos_basicos.get("nombres", "Desconocido"))

        nombres_str = ", ".join(nombres_procesados)
        return {"status": "success", "message": f"¡Éxito! Datos procesados correctamente: {nombres_str}."}

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
    response = supabase.table("personas").select(
        "created_at, numero_documento, nombres, apellidos, celular, correo_electronico, registros_ubicabilidad(fecha_carga, direccion, municipio_departamento, telefono, tipo_direccion, estado_direccion)"
    ).execute()

    datos = response.data
    if not datos:
        raise HTTPException(status_code=404, detail="No hay datos en la base de datos.")

    filas_excel = []
    for fila in datos:
        registros = fila.get("registros_ubicabilidad", [])
        fecha_creacion_persona = fila.get("created_at")[:10] if fila.get("created_at") else ""

        if not registros:
            filas_excel.append({
                "Fecha Carga": fecha_creacion_persona,
                "Documento": fila.get("numero_documento"),
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
                    "Documento": fila.get("numero_documento"),
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
