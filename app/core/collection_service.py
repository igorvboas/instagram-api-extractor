# app/core/collection_service.py
"""
Serviço de alto nível para coleta de mídias
Interface simplificada para uso na API
"""

import asyncio
from datetime import datetime
from typing import Dict, Any
from app.core.media_collector import MediaCollector
from app.core.account_pool import AccountPool
from app.config import Settings
from app.utils.logging_config import get_app_logger

logger = get_app_logger(__name__)


class CollectionService:
    """
    Serviço de coleta que gerencia MediaCollector e AccountPool
    Interface simplificada para a API
    """
    
    def __init__(self, settings: Settings):
        """
        Inicializa o serviço de coleta
        
        Args:
            settings: Configurações da aplicação
        """
        self.settings = settings
        self.account_pool = AccountPool(settings)
        self.media_collector = MediaCollector(self.account_pool, settings)
        
        logger.success("CollectionService inicializado")
    
    async def collect_user_content(self, username: str, 
                                 include_stories: bool = True,
                                 include_feed: bool = True,
                                 max_feed_posts: int = 10) -> Dict[str, Any]:
        """
        Coleta conteúdo de um usuário e retorna em formato otimizado para API
        
        Args:
            username: Nome de usuário do Instagram
            include_stories: Se deve incluir stories
            include_feed: Se deve incluir posts do feed
            max_feed_posts: Máximo de posts do feed
            
        Returns:
            Dicionário com dados prontos para resposta da API
        """
        logger.info(f"Iniciando coleta para @{username}")
        
        # Verificar se temos contas disponíveis
        available_accounts = len([acc for acc in self.account_pool.accounts if acc.is_available()])
        if available_accounts == 0:
            logger.error("Nenhuma conta disponível no pool")
            return {
                "success": False,
                "error": "Nenhuma conta disponível no pool",
                "username": username,
                "timestamp": datetime.now().isoformat()
            }
        
        # Executar coleta
        result = await self.media_collector.collect_user_media(
            username=username,
            include_stories=include_stories,
            include_feed=include_feed,
            max_feed_posts=max_feed_posts
        )
        
        # Converter para formato da API
        response_data = {
            "success": result.success,
            "username": result.username,
            "timestamp": result.timestamp.isoformat(),
            "account_used": result.account_used,
            "data": {
                "stories": [],
                "feed_posts": []
            }
        }
        
        if not result.success:
            response_data["error"] = result.error_message
            return response_data
        
        # Converter stories para formato da API
        for story in result.stories:
            story_data = {
                "id": story.id,
                "type": story.type,
                "filename": story.filename,
                "size_bytes": story.size_bytes,
                "binary_data": story.binary_data,  # Bytes raw para FastAPI converter
                "metadata": story.metadata
            }
            response_data["data"]["stories"].append(story_data)
        
        # Converter feed posts para formato da API
        for post in result.feed_posts:
            post_data = {
                "id": post.id,
                "type": post.type,
                "filename": post.filename,
                "size_bytes": post.size_bytes,
                "binary_data": post.binary_data,  # Bytes raw para FastAPI converter
                "metadata": post.metadata
            }
            response_data["data"]["feed_posts"].append(post_data)
        
        # Estatísticas
        total_files = len(response_data["data"]["stories"]) + len(response_data["data"]["feed_posts"])
        total_size_mb = sum(
            [story["size_bytes"] for story in response_data["data"]["stories"]] +
            [post["size_bytes"] for post in response_data["data"]["feed_posts"]]
        ) / (1024 * 1024)
        
        response_data["statistics"] = {
            "total_files": total_files,
            "total_size_mb": round(total_size_mb, 2),
            "stories_count": len(response_data["data"]["stories"]),
            "feed_posts_count": len(response_data["data"]["feed_posts"])
        }
        
        logger.success(f"Coleta concluída: {total_files} arquivos ({total_size_mb:.1f}MB)")
        
        return response_data
    
    def get_pool_status(self) -> Dict[str, Any]:
        """
        Retorna status do pool de contas
        
        Returns:
            Status detalhado do pool
        """
        return self.account_pool.get_pool_status()
    
    def cleanup(self):
        """
        Executa limpeza de recursos
        """
        self.media_collector.cleanup_temp_files()