import google.generativeai as genai

genai.configure(api_key="AIzaSyDDbXXTCaV5mUanSLWUiMIZJyYACyRFl0g")

print("Modelos habilitados para genereación de contenido: ")
for m in genai.list_models():
    if "generateContent" in m.supported_generation_methods:
        print(m.name)