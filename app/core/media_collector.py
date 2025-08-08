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
from typing import List, Optional, Dict, Any, Tuple
from pathlib import Path
import mimetypes
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone


from instagrapi import Client
from instagrapi.types import Story, Media, User
from instagrapi.exceptions import (
    UserNotFound, PrivateError, LoginRequired, 
    ChallengeRequired, RateLimitError, MediaNotFound,
    ClientError, PleaseWaitFewMinutes
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
        self.executor = ThreadPoolExecutor(max_workers=2)  # Para operações síncronas
        
        # Garantir path absoluto para temp_downloads
        from pathlib import Path
        project_root = Path(__file__).parent.parent.parent
        self.temp_dir = project_root / "data" / "temp_downloads"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"MediaCollector inicializado - temp_dir: {self.temp_dir}")
    
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
        feed_posts = []
        feed_files = []

        start_time = time.time()
        logger.info(f"Iniciando coleta para @{username}")
        
        # Validar entrada
        if not username or not isinstance(username, str):
            error_msg = "Nome de usuário inválido"
            logger.error(error_msg)
            return CollectionResult(
                username=username,
                success=False,
                error_message=error_msg
            )
        
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
                target_user = await self._get_user_info_safe(client, username)
                if not target_user:
                    raise UserNotFound(f"Usuário {username} não encontrado")
                    
                logger.info(f"Usuário encontrado: @{username} (ID: {target_user.pk})")
                
            except UserNotFound as e:
                error_msg = f"Usuário {username} não encontrado ou perfil privado"
                logger.warning(error_msg)
                self.pool.mark_account_used(account, success=True)  # Não é erro da conta
                result.success = False
                result.error_message = error_msg
                return result
                
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
                logger.info(f"Coletando stories de @{username}")
                try:
                    stories = await self._collect_stories_safe(client, target_user.pk)
                    if stories:
                        logger.success(f"Encontrados {len(stories)} stories")
                        
                        # Download dos stories
                        story_files = await self._download_stories_safe(client, stories, username)
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
                logger.info(f"Coletando posts das últimas 24h de @{username} (max: {max_feed_posts})")
                logger.success(f"Encontrados {len(feed_posts)} posts das últimas 24h")
                try:
                    feed_posts = await self._collect_feed_posts_safe(client, target_user.pk, max_feed_posts)
                    if feed_posts:
                        logger.success(f"Encontrados {len(feed_posts)} posts das últimas 24h")
                        
                        # Download dos posts
                        feed_files = await self._download_feed_posts_safe(client, feed_posts, username)
                        result.feed_posts = feed_files
                        
                        logger.info(f"[DEBUG] feed_files retornado: {len(feed_files)} arquivos (type={type(feed_files)})")

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
            
        except (RateLimitError, PleaseWaitFewMinutes):
            error_msg = f"Rate limit atingido para {account.username}"
            logger.warning(error_msg)
            account.status = AccountStatus.COOLDOWN
            self.pool.mark_account_used(account, success=False)
            result.success = False
            result.error_message = error_msg
            return result
            
        except LoginRequired:
            error_msg = f"Login requerido para conta {account.username}"
            logger.error(error_msg)
            account.status = AccountStatus.ERROR
            self.pool.mark_account_used(account, success=False)
            result.success = False
            result.error_message = error_msg
            return result
            
        except Exception as e:
            error_msg = f"Erro inesperado na coleta: {type(e).__name__}: {str(e)}"
            logger.error(error_msg)
            self.pool.mark_account_used(account, success=False)
            result.success = False
            result.error_message = error_msg
            return result
    
    async def _get_user_info_safe(self, client: Client, username: str) -> Optional[User]:
        """
        Obtém informações do usuário de forma segura usando ThreadPoolExecutor
        
        Args:
            client: Cliente Instagram
            username: Nome de usuário
            
        Returns:
            User object ou None se não encontrado
        """
        def _get_user_sync():
            try:
                # Tentar método principal
                user = client.user_info_by_username(username)
                return user, None
            except Exception as e:
                logger.warning(f"Método principal falhou, tentando alternativo: {e}")
                try:
                    user_id = client.user_id_from_username(username)
                    user = client.user_info(user_id)
                    return user, None
                except Exception as e2:
                    logger.error(f"Ambos os métodos falharam: {e2}")
                    return None, str(e2)
        
        try:
            await self._random_delay(0.5, 1.5)
            user, error = await asyncio.get_event_loop().run_in_executor(
                self.executor, _get_user_sync
            )
            
            if error and "not found" in error.lower():
                raise UserNotFound(error)
            elif error:
                raise Exception(error)
                
            return user
            
        except Exception as e:
            logger.error(f"Erro ao obter informações do usuário {username}: {e}")
            raise
    
    async def _collect_stories_safe(self, client: Client, user_id: int) -> List[Story]:
        """
        Coleta stories de um usuário de forma segura
        
        Args:
            client: Cliente Instagram
            user_id: ID do usuário
            
        Returns:
            Lista de stories
        """
        def _get_stories_sync():
            try:
                stories = client.user_stories(user_id)
                return stories or [], None
            except Exception as e:
                return [], str(e)
        
        try:
            await self._random_delay()
            stories, error = await asyncio.get_event_loop().run_in_executor(
                self.executor, _get_stories_sync
            )
            
            if error:
                if "login_required" in error.lower():
                    raise LoginRequired(error)
                else:
                    logger.warning(f"Erro ao coletar stories: {error}")
                    return []
            
            return stories
            
        except Exception as e:
            logger.warning(f"Erro ao coletar stories: {e}")
            return []
    
    async def _collect_feed_posts_safe(self, client: Client, user_id: int, max_posts: int) -> List[Media]:
        """
        Coleta posts do feed de um usuário das últimas 24 horas de forma segura
        
        Args:
            client: Cliente Instagram
            user_id: ID do usuário
            max_posts: Máximo de posts para coletar
            
        Returns:
            Lista de posts do feed das últimas 24h
        """
        def _get_feed_sync():
            try:
                search_limit = min(max_posts * 5, 50)
                try:
                    all_medias = client.user_medias(user_id, amount=search_limit)
                except Exception as e:
                    logger.warning(f"Falha ao coletar mídias: {e}")
                    # Continue mesmo se der erro, pode ser só warning/parsing
                    all_medias = []
                return all_medias or [], None
            except Exception as e:
                return [], str(e)
        
        try:
            await self._random_delay()
            all_medias, error = await asyncio.get_event_loop().run_in_executor(
                self.executor, _get_feed_sync
            )
            
            if error:
                if "login_required" in error.lower():
                    raise LoginRequired(error)
                else:
                    logger.warning(f"Erro ao coletar feed posts: {error}")
                    return []
            
            if not all_medias:
                return []
            
            # Filtrar posts das últimas 24 horas
            # Old calculation => twenty_four_hours_ago = datetime.now() - timedelta(hours=24)
            # Agora, usando UTC (offset-aware)
            twenty_four_hours_ago = datetime.now(timezone.utc) - timedelta(hours=24)
            
            recent_posts = []
            print(f"[DEBUG] user_medias retornou: {len(all_medias)} posts")
            print(f"[DEBUG] twenty_four_hours_ago = {twenty_four_hours_ago}")
            for media in all_medias:
                print(f"[DEBUG] media.taken_at = {media.taken_at}")
                # Verificar se o post foi feito nas últimas 24h
                if media.taken_at and media.taken_at >= twenty_four_hours_ago:
                    print("[DEBUG] --> Vai entrar no filtro!")
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
    
    async def _download_stories_safe(self, client: Client, stories: List[Story], username: str) -> List[MediaFile]:
        """
        Baixa arquivos dos stories de forma segura
        
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
                logger.info(f"Baixando story {i}/{len(stories)}")
                
                # Delay entre downloads
                if i > 1:
                    await self._random_delay(1.0, 2.0)
                
                media_file = await self._download_story_file_safe(client, story, username)
                if media_file:
                    media_files.append(media_file)
                    
            except Exception as e:
                if "login_required" in str(e).lower():
                    logger.error(f"Login requerido ao baixar story {story.pk}: {e}")
                    raise LoginRequired(str(e))
                else:
                    logger.warning(f"Erro ao baixar story {story.pk}: {e}")
                    continue
        
        return media_files
    
    async def _download_feed_posts_safe(self, client: Client, posts: List[Media], username: str) -> List[MediaFile]:
        """
        Baixa arquivos dos posts do feed de forma segura
        
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
                logger.info(f"Baixando post {i}/{len(posts)}")
                
                # Delay entre downloads
                if i > 1:
                    await self._random_delay(1.0, 2.0)
                
                # Posts podem ter múltiplas mídias (carrossel)
                if post.media_type == 8:  # Carrossel
                    carousel_files = await self._download_carousel_post_safe(client, post, username)
                    media_files.extend(carousel_files)
                else:
                    # Post simples (foto ou vídeo)
                    media_file = await self._download_single_post_safe(client, post, username)
                    if media_file:
                        media_files.append(media_file)
                        
            except Exception as e:
                if "login_required" in str(e).lower():
                    logger.error(f"Login requerido ao baixar post {post.pk}: {e}")
                    raise LoginRequired(str(e))
                else:
                    logger.warning(f"Erro ao baixar post {post.pk}: {e}")
                    continue
        
        return media_files
    
    async def _download_story_file_safe(self, client: Client, story: Story, username: str) -> Optional[MediaFile]:
        """
        Baixa arquivo individual de story de forma segura
        
        Args:
            client: Cliente Instagram
            story: Story object
            username: Nome do usuário
            
        Returns:
            MediaFile com dados binários ou None se erro
        """
        def _download_story_sync():
            try:
                temp_file = None
                media_type = None
                
                if story.media_type == 1:  # Foto
                    temp_file = client.photo_download(story.pk, folder=str(self.temp_dir))
                    media_type = MediaType.IMAGE
                elif story.media_type == 2:  # Vídeo
                    temp_file = client.video_download(story.pk, folder=str(self.temp_dir))
                    media_type = MediaType.VIDEO
                else:
                    return None, f"Tipo de story não suportado: {story.media_type}", None
                
                if not temp_file or not os.path.exists(temp_file):
                    return None, f"Arquivo não baixado: {story.pk}", None
                
                return temp_file, None, media_type
                
            except Exception as e:
                return None, str(e), None
        
        try:
            temp_file, error, media_type = await asyncio.get_event_loop().run_in_executor(
                self.executor, _download_story_sync
            )
            
            if error:
                if "login_required" in error.lower():
                    raise LoginRequired(error)
                else:
                    logger.warning(f"Erro no download do story: {error}")
                    return None
            
            if not temp_file:
                return None
            
            # Ler dados binários
            try:
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
                logger.error(f"Erro ao processar arquivo baixado {temp_file}: {e}")
                # Tentar remover arquivo mesmo em caso de erro
                try:
                    if os.path.exists(temp_file):
                        os.remove(temp_file)
                except:
                    pass
                return None
            
        except Exception as e:
            logger.error(f"Erro ao baixar story {story.pk}: {e}")
            return None
    
    async def _download_single_post_safe(self, client: Client, post: Media, username: str) -> Optional[MediaFile]:
        """
        Baixa arquivo individual de post de forma segura
        
        Args:
            client: Cliente Instagram
            post: Media object
            username: Nome do usuário
            
        Returns:
            MediaFile com dados binários ou None se erro
        """
        def _download_post_sync():
            try:
                temp_file = None
                media_type = None
                
                if post.media_type == 1:  # Foto
                    temp_file = client.photo_download(post.pk, folder=str(self.temp_dir))
                    media_type = MediaType.IMAGE
                elif post.media_type == 2:  # Vídeo/IGTV/Reels
                    temp_file = client.video_download(post.pk, folder=str(self.temp_dir))
                    media_type = MediaType.VIDEO
                else:
                    return None, f"Tipo de post não suportado: {post.media_type}", None
                
                if not temp_file or not os.path.exists(temp_file):
                    return None, f"Arquivo não baixado: {post.pk}", None
                
                return temp_file, None, media_type
                
            except Exception as e:
                return None, str(e), None
        
        try:
            temp_file, error, media_type = await asyncio.get_event_loop().run_in_executor(
                self.executor, _download_post_sync
            )
            
            if error:
                if "login_required" in error.lower():
                    raise LoginRequired(error)
                else:
                    logger.warning(f"Erro no download do post: {error}")
                    return None
            
            if not temp_file:
                return None
            
            # Ler dados binários
            try:
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
                    # Use datetime.now(timezone.utc) para garantir timezone compatível
                    hours_old = (datetime.now(timezone.utc) - post.taken_at).total_seconds() / 3600
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
                logger.error(f"Erro ao processar arquivo baixado {temp_file}: {e}")
                # Tentar remover arquivo mesmo em caso de erro
                try:
                    if os.path.exists(temp_file):
                        os.remove(temp_file)
                except:
                    pass
                return None
            
        except Exception as e:
            logger.error(f"Erro ao baixar post {post.pk}: {e}")
            return None
    
    async def _download_carousel_post_safe(self, client: Client, post: Media, username: str) -> List[MediaFile]:
        """
        Baixa arquivos de post carrossel (múltiplas imagens/vídeos) de forma segura
        
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
                    def _download_carousel_item_sync():
                        try:
                            temp_file = None
                            media_type = None
                            
                            if resource.media_type == 1:  # Foto
                                temp_file = client.photo_download(resource.pk, folder=str(self.temp_dir))
                                media_type = MediaType.IMAGE
                            elif resource.media_type == 2:  # Vídeo
                                temp_file = client.video_download(resource.pk, folder=str(self.temp_dir))
                                media_type = MediaType.VIDEO
                            else:
                                return None, f"Tipo não suportado: {resource.media_type}", None
                            
                            if not temp_file or not os.path.exists(temp_file):
                                return None, f"Arquivo não baixado: {resource.pk}", None
                            
                            return temp_file, None, media_type
                            
                        except Exception as e:
                            return None, str(e), None
                    
                    temp_file, error, media_type = await asyncio.get_event_loop().run_in_executor(
                        self.executor, _download_carousel_item_sync
                    )
                    
                    if error or not temp_file:
                        logger.warning(f"Erro ao baixar item {i+1} do carrossel: {error}")
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
    
    def __del__(self):
        """Limpa o ThreadPoolExecutor quando o objeto é destruído"""
        if hasattr(self, 'executor'):
            self.executor.shutdown(wait=False)