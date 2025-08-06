#!/usr/bin/env python3
"""
Script para executar a Instagram Collection API
"""

import sys
import os

# Adicionar diretório raiz ao path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.main import app
import uvicorn
from app.config import Settings

if __name__ == "__main__":
    settings = Settings()
    
    print("🚀 Instagram Collection API")
    print("="*50)
    print(f"🌐 Host: {settings.api_host}")
    print(f"🔌 Port: {settings.api_port}")
    print(f"📁 Sessions: {settings.session_dir}")
    print(f"📊 Max accounts: {settings.max_accounts}")
    print("="*50)
    
    uvicorn.run(
        "app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=True,
        log_level="info"
    )
