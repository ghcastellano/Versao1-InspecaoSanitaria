import os
import io
import json
import logging
import structlog
import httpx
import vertexai
from datetime import datetime
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload, MediaFileUpload
from vertexai.generative_models import GenerativeModel, Part, HarmCategory, HarmBlockThreshold
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML, CSS
from dotenv import load_dotenv

import models

# 1. Configuração de Logs
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer()
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
)
logger = structlog.get_logger()

# 2. Carregamento de Configurações
load_dotenv()

# Configuração GCP
CREDENTIALS_FILE = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json")
PROJECT_ID = os.getenv("GCP_PROJECT_ID")
LOCATION = os.getenv("GCP_LOCATION", "us-central1")

# Configuração Drive
FOLDER_IN = os.getenv("FOLDER_ID_01_ENTRADA_RELATORIOS")
FOLDER_OUT = os.getenv("FOLDER_ID_02_PLANOS_GERADOS")
FOLDER_BACKUP = os.getenv("FOLDER_ID_03_PROCESSADOS_BACKUP")
FOLDER_ERROR = os.getenv("FOLDER_ID_99_ERROS")

# Configuração WhatsApp
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")

class Processor:
    def __init__(self):
        self.creds = Credentials.from_service_account_file(CREDENTIALS_FILE)
        self.drive_service = build('drive', 'v3', credentials=self.creds)
        
        # Inicializa Vertex AI
        vertexai.init(project=PROJECT_ID, location=LOCATION)
        self.model = GenerativeModel("gemini-1.5-flash-001")
        
        # Inicializa Jinja
        self.jinja_env = Environment(loader=FileSystemLoader('src/templates'))

    def list_pending_files(self):
        """Lista arquivos na pasta de entrada."""
        query = f"'{FOLDER_IN}' in parents and trashed=false and mimeType='application/pdf'"
        results = self.drive_service.files().list(q=query, fields="files(id, name)").execute()
        return results.get('files', [])

    def download_file(self, file_id) -> bytes:
        """Baixa um arquivo como bytes."""
        request = self.drive_service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while done is False:
            status, done = downloader.next_chunk()
        return fh.getvalue()

    def analyze_with_gemini(self, file_bytes: bytes) -> models.RelatorioInspecao:
        """Envia bytes do PDF para o Gemini e faz o parse da resposta."""
        prompt = """
        Você é um Engenheiro Sanitário Sênior. Sua tarefa é analisar este relatório de inspeção sanitária anexado.
        
        Objetivos:
        1. Identificar todas as Não Conformidades citadas.
        2. Para cada não conformidade, extrair o item/área e descrever o problema.
        3. Propor ações corretivas práticas baseadas estritamente na legislação brasileira RDC 216 da ANVISA.
        4. Classificar a prioridade (Alta, Média, Baixa) e sugerir um prazo.
        5. Criar um resumo geral da situação do estabelecimento.
        6. Identificar pontos fortes, se houver.
        
        Retorne a resposta EXCLUSIVAMENTE em formato JSON, seguindo este schema:
        """
        
        # Converte modelo Pydantic para string de schema para contexto do prompt
        schema_json = json.dumps(models.RelatorioInspecao.model_json_schema(), ensure_ascii=False)
        prompt += f"\n{schema_json}"
        
        file_part = Part.from_data(data=file_bytes, mime_type="application/pdf")
        
        response = self.model.generate_content(
            [file_part, prompt],
            generation_config={"response_mime_type": "application/json"},
            safety_settings={
                HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_ONLY_HIGH,
                HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
                HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
                HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_ONLY_HIGH
            }
        )
        
        try:
            json_data = json.loads(response.text)
            return models.RelatorioInspecao(**json_data)
        except Exception as e:
            logger.error("Falha ao processar resposta do Gemini", error=str(e), raw_response=response.text)
            raise

    def generate_pdf(self, data: models.RelatorioInspecao, filename: str) -> str:
        """Renderiza HTML e converte para PDF."""
        template = self.jinja_env.get_template('base_layout.html')
        html_out = template.render(dados=data)
        
        output_path = f"/tmp/{filename}"
        HTML(string=html_out, base_url="src/templates").write_pdf(
            output_path, 
            stylesheets=[CSS('src/templates/style.css')]
        )
        return output_path

    def upload_file(self, local_path: str, filename: str) -> str:
        """Faz upload do arquivo para a pasta de saída e torna público (qualquer um com link)."""
        file_metadata = {
            'name': filename,
            'parents': [FOLDER_OUT]
        }
        media = MediaFileUpload(local_path, mimetype='application/pdf')
        file = self.drive_service.files().create(
            body=file_metadata, media_body=media, fields='id, webViewLink'
        ).execute()
        
        file_id = file.get('id')
        
        # Tornar público (leitor)
        permission = {
            'type': 'anyone',
            'role': 'reader'
        }
        self.drive_service.permissions().create(
            fileId=file_id,
            body=permission,
            fields='id',
        ).execute()
        
        return file.get('webViewLink')

    def move_file(self, file_id, folder_destination_id):
        """Move um arquivo adicionando o novo pai e removendo os antigos."""
        # Recupera pais anteriores
        file = self.drive_service.files().get(fileId=file_id, fields='parents').execute()
        previous_parents = ",".join(file.get('parents'))
        
        self.drive_service.files().update(
            fileId=file_id,
            addParents=folder_destination_id,
            removeParents=previous_parents,
            fields='id, parents'
        ).execute()

    def send_whatsapp_notification(self, link: str):
        """Envia notificação via WhatsApp Cloud API."""
        if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_ID:
            logger.warning("Credenciais do WhatsApp não encontradas. Pulando notificação.")
            return

        url = f"https://graph.facebook.com/v17.0/{WHATSAPP_PHONE_ID}/messages"
        headers = {
            "Authorization": f"Bearer {WHATSAPP_TOKEN}",
            "Content-Type": "application/json"
        }
        
        # Destino hardcoded para MVP. Idealmente vem do arquivo ou conteúdo.
        DESTINATION_PHONE = os.getenv("WHATSAPP_DESTINATION_PHONE") 
        
        if not DESTINATION_PHONE:
            logger.warning("Nenhum número de destino configurado (WHATSAPP_DESTINATION_PHONE).")
            return

        data = {
            "messaging_product": "whatsapp",
            "to": DESTINATION_PHONE,
            "type": "text",
            "text": {
                "body": f"Seu Plano de Ação está pronto: {link}"
            }
        }
        
        try:
            response = httpx.post(url, headers=headers, json=data, timeout=10)
            response.raise_for_status()
            logger.info("Notificação WhatsApp enviada", phone=DESTINATION_PHONE)
        except Exception as e:
            logger.error("Falha ao enviar WhatsApp", error=str(e))

    def run(self):
        logger.info("Iniciando job de processamento")
        
        files = self.list_pending_files()
        logger.info(f"Encontrados {len(files)} arquivos para processar")
        
        for file in files:
            file_id = file['id']
            filename = file['name']
            logger.info("Processando arquivo", filename=filename, file_id=file_id)
            
            try:
                # 1. Download
                pdf_bytes = self.download_file(file_id)
                
                # 2. Analisar
                report_data = self.analyze_with_gemini(pdf_bytes)
                
                # 3. Gerar PDF
                output_filename = f"Plano_Acao_{filename}"
                local_pdf_path = self.generate_pdf(report_data, output_filename)
                
                # 4. Upload
                public_link = self.upload_file(local_pdf_path, output_filename)
                logger.info("Relatório gerado", link=public_link)
                
                # 5. Mover Original
                self.move_file(file_id, FOLDER_BACKUP)
                
                # 6. Notificar
                self.send_whatsapp_notification(public_link)
                
            except Exception as e:
                logger.error("Erro ao processar arquivo", filename=filename, error=str(e))
                # Mover para pasta de Erro
                try:
                    self.move_file(file_id, FOLDER_ERROR)
                except:
                    pass

if __name__ == "__main__":
    processor = Processor()
    processor.run()
