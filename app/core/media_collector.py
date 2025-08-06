# app/core/media_collector.py
"""
Sistema de coleta de mídias do Instagram (Stories + Feed)
Otimizado para performance e compatibilidade com N8N
"""

import os
import asyncio
import time
import random
import tempfile
import hashlib
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any, Tuple
from pathlib import Path
import mimetypes

from instagrapi import Client
from instagrapi.types import Story, Media, User
from instagrapi.exceptions import (
    UserNotFound, PrivateError, LoginRequired, 
    ChallengeRequired, RateLimitError
)

from app.config import Settings
from app.models import InstagramAccount, MediaFile, CollectionResult, MediaType, AccountStatus
from app.core.account_pool import AccountPool
from app.utils.logging_config import get_app_logger

logger = get_app_logger(__name__)


class MediaCollector:
    """
    Coletor de mídias do Instagram com otimizações para API
    """
    
    def __init__(self, account_pool: AccountPool, settings: Settings):
        """
        Inicializa o coletor de mídias
        
        Args:
            account_pool: Pool de contas Instagram
            settings: Configurações da aplicação
        """
        self.pool = account_pool
        self.settings = settings
        
        # Garantir path absoluto para temp_downloads
        from pathlib import Path
        project_root = Path(__file__).parent.parent.parent
        self.temp_dir = project_root / "data" / "temp_downloads"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"MediaCollector inicializado - temp_dir: {self.temp_dir}")
        logger.info("MediaCollector inicializado")
    
    async def collect_user_media(self, username: str, include_stories: bool = True, 
                                include_feed: bool = True, max_feed_posts: int = 10) -> CollectionResult:
        """
        Coleta mídias de um usuário (stories + feed posts)
        
        Args:
            username: Nome de usuário do Instagram
            include_stories: Se deve incluir stories
            include_feed: Se deve incluir posts do feed
            max_feed_posts: Máximo de posts do feed para coletar
            
        Returns:
            CollectionResult com mídias coletadas
        """
        start_time = time.time()
        logger.loading(f"Iniciando coleta para @{username}")
        
        # Obter conta disponível
        account = self.pool.get_available_account()
        if not account:
            error_msg = "Nenhuma conta disponível no pool"
            logger.error(error_msg)
            return CollectionResult(
                username=username,
                success=False,
                error_message=error_msg
            )
        
        # Obter cliente configurado
        client = self.pool.get_client(account)
        if not client:
            error_msg = f"Não foi possível obter cliente para {account.username}"
            logger.error(error_msg)
            return CollectionResult(
                username=username,
                success=False,
                error_message=error_msg
            )
        
        result = CollectionResult(username=username, account_used=account.username)
        
        try:
            # Obter informações do usuário target
            try:
                target_user = await self._get_user_info(client, username)
                if not target_user:
                    raise UserNotFound(f"Usuário {username} não encontrado")
                    
                logger.info(f"Usuário encontrado: @{username} (ID: {target_user.pk})")
                
            except Exception as e:
                error_msg = f"Erro ao buscar usuário {username}: {str(e)}"
                logger.error(error_msg)
                self.pool.mark_account_used(account, success=False)
                result.success = False
                result.error_message = error_msg
                return result
            
            # Adicionar delay entre operações
            await self._random_delay()
            
            # Coletar stories se solicitado
            if include_stories:
                logger.loading(f"Coletando stories de @{username}")
                try:
                    stories = await self._collect_stories(client, target_user.pk)
                    if stories:
                        logger.success(f"Encontrados {len(stories)} stories")
                        
                        # Download dos stories
                        story_files = await self._download_stories(client, stories, username)
                        result.stories = story_files
                        
                        logger.success(f"Downloaded {len(story_files)} story files")
                    else:
                        logger.info(f"Nenhum story encontrado para @{username}")
                        
                except Exception as e:
                    logger.warning(f"Erro ao coletar stories: {str(e)}")
                    # Não falha a operação inteira por erro nos stories
            
            # Adicionar delay entre stories e feed
            await self._random_delay()
            
            # Coletar feed posts se solicitado
            if include_feed:
                logger.loading(f"Coletando posts das últimas 24h de @{username} (max: {max_feed_posts})")
                try:
                    feed_posts = await self._collect_feed_posts(client, target_user.pk, max_feed_posts)
                    if feed_posts:
                        logger.success(f"Encontrados {len(feed_posts)} posts das últimas 24h")
                        
                        # Download dos posts
                        feed_files = await self._download_feed_posts(client, feed_posts, username)
                        result.feed_posts = feed_files
                        
                        logger.success(f"Downloaded {len(feed_files)} feed files das últimas 24h")
                    else:
                        logger.info(f"Nenhum post das últimas 24h encontrado para @{username}")
                        
                except Exception as e:
                    logger.warning(f"Erro ao coletar feed: {str(e)}")
                    # Não falha a operação inteira por erro no feed
            
            # Marcar conta como usada com sucesso
            self.pool.mark_account_used(account, success=True)
            
            # Estatísticas finais
            total_files = len(result.stories) + len(result.feed_posts)
            total_time = time.time() - start_time
            
            logger.success(f"Coleta concluída para @{username}: {total_files} arquivos em {total_time:.1f}s")
            
            result.success = True
            return result
            
        except PrivateError:
            error_msg = f"Perfil @{username} é privado"
            logger.warning(error_msg)
            self.pool.mark_account_used(account, success=True)  # Não é erro da conta
            result.success = False
            result.error_message = error_msg
            return result
            
        except RateLimitError:
            error_msg = f"Rate limit atingido para {account.username}"
            logger.warning(error_msg)
            account.status = AccountStatus.COOLDOWN
            self.pool.mark_account_used(account, success=False)
            result.success = False
            result.error_message = error_msg
            return result
            
        except Exception as e:
            error_msg = f"Erro inesperado na coleta: {str(e)}"
            logger.error(error_msg)
            self.pool.mark_account_used(account, success=False)
            result.success = False
            result.error_message = error_msg
            return result
    
    async def _get_user_info(self, client: Client, username: str) -> Optional[User]:
        """
        Obtém informações do usuário de forma segura
        
        Args:
            client: Cliente Instagram
            username: Nome de usuário
            
        Returns:
            User object ou None se não encontrado
        """
        try:
            await self._random_delay(0.5, 1.5)
            user = client.user_info_by_username(username)
            return user
        except Exception as e:
            logger.warning(f"Método principal falhou, tentando alternativo: {e}")
            try:
                user_id = client.user_id_from_username(username)
                user = client.user_info(user_id)
                return user
            except Exception as e2:
                logger.error(f"Ambos os métodos falharam: {e2}")
                return None
    
    async def _collect_stories(self, client: Client, user_id: int) -> List[Story]:
        """
        Coleta stories de um usuário
        
        Args:
            client: Cliente Instagram
            user_id: ID do usuário
            
        Returns:
            Lista de stories
        """
        try:
            await self._random_delay()
            stories = client.user_stories(user_id)
            return stories or []
        except Exception as e:
            logger.warning(f"Erro ao coletar stories: {e}")
            return []
    
    async def _collect_feed_posts(self, client: Client, user_id: int, max_posts: int) -> List[Media]:
        """
        Coleta posts do feed de um usuário das últimas 24 horas
        
        Args:
            client: Cliente Instagram
            user_id: ID do usuário
            max_posts: Máximo de posts para coletar (usado como limite de busca)
            
        Returns:
            Lista de posts do feed das últimas 24h
        """
        try:
            await self._random_delay()
            
            # Buscar mais posts para filtrar por data (até 50)
            search_limit = min(max_posts * 5, 50)  # Buscar 5x mais para filtrar
            all_medias = client.user_medias(user_id, amount=search_limit)
            
            if not all_medias:
                return []
            
            # Filtrar posts das últimas 24 horas
            from datetime import datetime, timedelta
            
            # Calcular timestamp de 24 horas atrás
            twenty_four_hours_ago = datetime.now() - timedelta(hours=24)
            
            recent_posts = []
            for media in all_medias:
                # Verificar se o post foi feito nas últimas 24h
                if media.taken_at and media.taken_at >= twenty_four_hours_ago:
                    recent_posts.append(media)
                    # Se já temos posts suficientes, parar
                    if len(recent_posts) >= max_posts:
                        break
                else:
                    # Como os posts vêm em ordem cronológica reversa,
                    # se encontramos um post mais antigo que 24h, podemos parar
                    break
            
            logger.info(f"Posts filtrados: {len(recent_posts)} dos últimos {len(all_medias)} posts são das últimas 24h")
            return recent_posts
            
        except Exception as e:
            logger.warning(f"Erro ao coletar feed posts: {e}")
            return []
    
    async def _download_stories(self, client: Client, stories: List[Story], username: str) -> List[MediaFile]:
        """
        Baixa arquivos dos stories
        
        Args:
            client: Cliente Instagram
            stories: Lista de stories
            username: Nome do usuário (para organização)
            
        Returns:
            Lista de MediaFile com dados binários
        """
        media_files = []
        
        for i, story in enumerate(stories, 1):
            try:
                logger.loading(f"Baixando story {i}/{len(stories)}")
                
                # Delay entre downloads
                if i > 1:
                    await self._random_delay(1.0, 2.0)
                
                media_file = await self._download_story_file(client, story, username)
                if media_file:
                    media_files.append(media_file)
                    
            except Exception as e:
                logger.warning(f"Erro ao baixar story {story.pk}: {e}")
                continue
        
        return media_files
    
    async def _download_feed_posts(self, client: Client, posts: List[Media], username: str) -> List[MediaFile]:
        """
        Baixa arquivos dos posts do feed
        
        Args:
            client: Cliente Instagram
            posts: Lista de posts
            username: Nome do usuário (para organização)
            
        Returns:
            Lista de MediaFile com dados binários
        """
        media_files = []
        
        for i, post in enumerate(posts, 1):
            try:
                logger.loading(f"Baixando post {i}/{len(posts)}")
                
                # Delay entre downloads
                if i > 1:
                    await self._random_delay(1.0, 2.0)
                
                # Posts podem ter múltiplas mídias (carrossel)
                if post.media_type == 8:  # Carrossel
                    carousel_files = await self._download_carousel_post(client, post, username)
                    media_files.extend(carousel_files)
                else:
                    # Post simples (foto ou vídeo)
                    media_file = await self._download_single_post(client, post, username)
                    if media_file:
                        media_files.append(media_file)
                        
            except Exception as e:
                logger.warning(f"Erro ao baixar post {post.pk}: {e}")
                continue
        
        return media_files
    
    async def _download_story_file(self, client: Client, story: Story, username: str) -> Optional[MediaFile]:
        """
        Baixa arquivo individual de story
        
        Args:
            client: Cliente Instagram
            story: Story object
            username: Nome do usuário
            
        Returns:
            MediaFile com dados binários ou None se erro
        """
        try:
            temp_file = None
            
            if story.media_type == 1:  # Foto
                temp_file = client.photo_download(story.pk, folder=self.temp_dir)
                media_type = MediaType.IMAGE
            elif story.media_type == 2:  # Vídeo
                temp_file = client.video_download(story.pk, folder=self.temp_dir)
                media_type = MediaType.VIDEO
            else:
                logger.warning(f"Tipo de story não suportado: {story.media_type}")
                return None
            
            if not temp_file or not os.path.exists(temp_file):
                logger.warning(f"Arquivo não baixado: {story.pk}")
                return None
            
            # Ler dados binários
            with open(temp_file, 'rb') as f:
                binary_data = f.read()
            
            # Remover arquivo temporário
            os.remove(temp_file)
            
            # Criar MediaFile
            filename = f"story_{story.pk}_{username}.{self._get_file_extension(media_type)}"
            
            metadata = {
                "story_id": story.pk,
                "taken_at": story.taken_at.isoformat() if story.taken_at else None,
                "media_type": story.media_type,
                "username": username,
                "is_story": True
            }
            
            # Adicionar metadados específicos se disponíveis
            if hasattr(story, 'video_duration') and story.video_duration:
                metadata["duration_seconds"] = story.video_duration
                
            if hasattr(story, 'caption_text') and story.caption_text:
                metadata["caption"] = story.caption_text
            
            media_file = MediaFile(
                id=story.pk,
                type=media_type,
                binary_data=binary_data,
                filename=filename,
                size_bytes=len(binary_data),
                metadata=metadata
            )
            
            return media_file
            
        except Exception as e:
            logger.error(f"Erro ao baixar story {story.pk}: {e}")
            return None
    
    async def _download_single_post(self, client: Client, post: Media, username: str) -> Optional[MediaFile]:
        """
        Baixa arquivo individual de post
        
        Args:
            client: Cliente Instagram
            post: Media object
            username: Nome do usuário
            
        Returns:
            MediaFile com dados binários ou None se erro
        """
        try:
            temp_file = None
            
            if post.media_type == 1:  # Foto
                temp_file = client.photo_download(post.pk, folder=self.temp_dir)
                media_type = MediaType.IMAGE
            elif post.media_type == 2:  # Vídeo/IGTV/Reels
                temp_file = client.video_download(post.pk, folder=self.temp_dir)
                media_type = MediaType.VIDEO
            else:
                logger.warning(f"Tipo de post não suportado: {post.media_type}")
                return None
            
            if not temp_file or not os.path.exists(temp_file):
                logger.warning(f"Arquivo não baixado: {post.pk}")
                return None
            
            # Ler dados binários
            with open(temp_file, 'rb') as f:
                binary_data = f.read()
            
            # Remover arquivo temporário
            os.remove(temp_file)
            
            # Criar MediaFile
            filename = f"post_{post.pk}_{username}.{self._get_file_extension(media_type)}"
            
            metadata = {
                "post_id": post.pk,
                "taken_at": post.taken_at.isoformat() if post.taken_at else None,
                "media_type": post.media_type,
                "username": username,
                "is_story": False,
                "like_count": getattr(post, 'like_count', 0),
                "comment_count": getattr(post, 'comment_count', 0)
            }
            
            # Adicionar informação sobre idade do post
            if post.taken_at:
                from datetime import datetime
                hours_old = (datetime.now() - post.taken_at).total_seconds() / 3600
                metadata["hours_old"] = round(hours_old, 1)
                metadata["is_recent"] = hours_old <= 24
            
            # Adicionar metadados específicos
            if hasattr(post, 'video_duration') and post.video_duration:
                metadata["duration_seconds"] = post.video_duration
                
            if hasattr(post, 'caption_text') and post.caption_text:
                metadata["caption"] = post.caption_text[:500]  # Limitar caption
            
            media_file = MediaFile(
                id=post.pk,
                type=media_type,
                binary_data=binary_data,
                filename=filename,
                size_bytes=len(binary_data),
                metadata=metadata
            )
            
            return media_file
            
        except Exception as e:
            logger.error(f"Erro ao baixar post {post.pk}: {e}")
            return None
    
    async def _download_carousel_post(self, client: Client, post: Media, username: str) -> List[MediaFile]:
        """
        Baixa arquivos de post carrossel (múltiplas imagens/vídeos)
        
        Args:
            client: Cliente Instagram
            post: Media object (carrossel)
            username: Nome do usuário
            
        Returns:
            Lista de MediaFile
        """
        media_files = []
        
        try:
            # Obter recursos do carrossel
            resources = getattr(post, 'resources', [])
            if not resources:
                logger.warning(f"Carrossel {post.pk} sem recursos")
                return media_files
            
            for i, resource in enumerate(resources):
                try:
                    temp_file = None
                    
                    if resource.media_type == 1:  # Foto
                        temp_file = client.photo_download(resource.pk, folder=self.temp_dir)
                        media_type = MediaType.IMAGE
                    elif resource.media_type == 2:  # Vídeo
                        temp_file = client.video_download(resource.pk, folder=self.temp_dir)
                        media_type = MediaType.VIDEO
                    else:
                        continue
                    
                    if not temp_file or not os.path.exists(temp_file):
                        continue
                    
                    # Ler dados binários
                    with open(temp_file, 'rb') as f:
                        binary_data = f.read()
                    
                    # Remover arquivo temporário
                    os.remove(temp_file)
                    
                    # Criar MediaFile
                    filename = f"carousel_{post.pk}_{i+1}_{username}.{self._get_file_extension(media_type)}"
                    
                    metadata = {
                        "post_id": post.pk,
                        "carousel_index": i + 1,
                        "carousel_total": len(resources),
                        "resource_id": resource.pk,
                        "taken_at": post.taken_at.isoformat() if post.taken_at else None,
                        "media_type": resource.media_type,
                        "username": username,
                        "is_story": False,
                        "is_carousel": True
                    }
                    
                    media_file = MediaFile(
                        id=f"{post.pk}_{i+1}",
                        type=media_type,
                        binary_data=binary_data,
                        filename=filename,
                        size_bytes=len(binary_data),
                        metadata=metadata
                    )
                    
                    media_files.append(media_file)
                    
                    # Delay entre itens do carrossel
                    if i < len(resources) - 1:
                        await self._random_delay(0.5, 1.0)
                        
                except Exception as e:
                    logger.warning(f"Erro ao baixar item {i+1} do carrossel {post.pk}: {e}")
                    continue
            
        except Exception as e:
            logger.error(f"Erro ao processar carrossel {post.pk}: {e}")
        
        return media_files
    
    def _get_file_extension(self, media_type: MediaType) -> str:
        """
        Retorna extensão de arquivo baseada no tipo de mídia
        
        Args:
            media_type: Tipo da mídia
            
        Returns:
            Extensão do arquivo
        """
        extensions = {
            MediaType.IMAGE: "jpg",
            MediaType.VIDEO: "mp4",
            MediaType.CAROUSEL: "jpg"  # Default para carrossel
        }
        return extensions.get(media_type, "bin")
    
    async def _random_delay(self, min_delay: float = None, max_delay: float = None):
        """
        Adiciona delay aleatório para simular comportamento humano
        
        Args:
            min_delay: Delay mínimo (usa configuração se None)
            max_delay: Delay máximo (usa configuração se None)
        """
        min_d = min_delay or self.settings.request_delay_min
        max_d = max_delay or self.settings.request_delay_max
        
        delay = random.uniform(min_d, max_d)
        await asyncio.sleep(delay)
    
    def cleanup_temp_files(self):
        """
        Limpa arquivos temporários antigos
        """
        try:
            now = time.time()
            cutoff = now - (3600)  # 1 hora
            
            for file_path in self.temp_dir.glob("*"):
                if file_path.is_file() and file_path.stat().st_mtime < cutoff:
                    file_path.unlink()
                    
            logger.info("Limpeza de arquivos temporários concluída")
            
        except Exception as e:
            logger.warning(f"Erro na limpeza de arquivos temporários: {e}")