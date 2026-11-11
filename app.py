import webview
from src.desktop.api import Api
from pathlib import Path

arquivo_html = Path(__file__).resolve().parent / 'index.html'

webview.create_window(
    title='DeliveryLens AI',          # Título da janela
    url=arquivo_html.as_uri(),       # URL remota ou caminho local (sem protocolo)
    html=None,      # Código HTML direto (precede o 'url' se ambos forem definidos)
    js_api=Api(),    # Objeto Python para expor ao JavaScript
    width=1366,      # Largura em pixels (padrão: 1366)
    height=768,     # Altura em pixels (padrão: 768)
    resizable=True, # Permite redimensionamento
    fullscreen=False, # Inicia em tela cheia
    frameless=False, # Remove bordas da janela
    hidden=False,   # Cria janela oculta inicialmente
    confirm_close=False, # Pede confirmação ao fechar
    server=None     # Servidor WSGI externo (ex: Flask)
)

webview.start()