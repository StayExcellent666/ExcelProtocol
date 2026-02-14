import aiohttp
import logging
from typing import List, Dict, Optional
from config import TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET

logger = logging.getLogger(__name__)

class TwitchAPI:
    def __init__(self):
        self.client_id = TWITCH_CLIENT_ID
        self.client_secret = TWITCH_CLIENT_SECRET
        self.access_token = None
        self.base_url = "https://api.twitch.tv/helix"
        self.session = None
    
    async def get_session(self):
        """Get or create aiohttp session"""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session
    
    async def close(self):
        """Close the aiohttp session"""
        if self.session and not self.session.closed:
            await self.session.close()
    
    async def get_app_access_token(self) -> str:
        """Get OAuth app access token from Twitch"""
        session = await self.get_session()
        
        url = "https://id.twitch.tv/oauth2/token"
        params = {
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'grant_type': 'client_credentials'
        }
        
        async with session.post(url, params=params) as response:
            if response.status == 200:
                data = await response.json()
                self.access_token = data['access_token']
                logger.info("Successfully obtained Twitch access token")
                return self.access_token
            else:
                error_text = await response.text()
                logger.error(f"Failed to get access token: {response.status} - {error_text}")
                raise Exception(f"Failed to get Twitch access token: {response.status}")
    
    async def ensure_token(self):
        """Ensure we have a valid access token"""
        if not self.access_token:
            await self.get_app_access_token()
    
    async def get_headers(self) -> dict:
        """Get headers for Twitch API requests"""
        await self.ensure_token()
        return {
            'Client-ID': self.client_id,
            'Authorization': f'Bearer {self.access_token}'
        }
    
    async def get_user(self, username: str) -> Optional[Dict]:
        """
        Get user information by username
        Returns user data or None if not found
        """
        session = await self.get_session()
        headers = await self.get_headers()
        
        url = f"{self.base_url}/users"
        params = {'login': username.lower()}
        
        try:
            async with session.get(url, headers=headers, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    if data['data']:
                        return data['data'][0]
                    return None
                elif response.status == 401:
                    # Token expired, get new one and retry
                    logger.warning("Access token expired, refreshing...")
                    self.access_token = None
                    headers = await self.get_headers()
                    async with session.get(url, headers=headers, params=params) as retry_response:
                        if retry_response.status == 200:
                            data = await retry_response.json()
                            if data['data']:
                                return data['data'][0]
                        return None
                else:
                    error_text = await response.text()
                    logger.error(f"Error getting user {username}: {response.status} - {error_text}")
                    return None
        except Exception as e:
            logger.error(f"Exception getting user {username}: {e}")
            return None
    
    async def get_live_streams(self, usernames: List[str]) -> List[Dict]:
        """
        Check which users are currently live
        Returns list of stream data for live streams
        Supports up to 100 usernames per call
        """
        if not usernames:
            return []
        
        session = await self.get_session()
        headers = await self.get_headers()
        
        url = f"{self.base_url}/streams"
        
        # Twitch API accepts up to 100 user_login parameters
        params = [('user_login', username.lower()) for username in usernames[:100]]
        
        try:
            async with session.get(url, headers=headers, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    # Get profile images for each stream
                    if data['data']:
                        user_ids = [stream['user_id'] for stream in data['data']]
                        profile_images = await self.get_profile_images(user_ids)
                        
                        # Add profile images to stream data
                        for stream in data['data']:
                            stream['profile_image_url'] = profile_images.get(stream['user_id'], '')
                    
                    return data['data']
                
                elif response.status == 401:
                    # Token expired, get new one and retry
                    logger.warning("Access token expired, refreshing...")
                    self.access_token = None
                    headers = await self.get_headers()
                    async with session.get(url, headers=headers, params=params) as retry_response:
                        if retry_response.status == 200:
                            data = await retry_response.json()
                            
                            if data['data']:
                                user_ids = [stream['user_id'] for stream in data['data']]
                                profile_images = await self.get_profile_images(user_ids)
                                
                                for stream in data['data']:
                                    stream['profile_image_url'] = profile_images.get(stream['user_id'], '')
                            
                            return data['data']
                        return []
                else:
                    error_text = await response.text()
                    logger.error(f"Error getting streams: {response.status} - {error_text}")
                    return []
        
        except Exception as e:
            logger.error(f"Exception getting streams: {e}")
            return []
    
    async def get_profile_images(self, user_ids: List[str]) -> Dict[str, str]:
        """
        Get profile images for multiple users
        Returns dict mapping user_id to profile_image_url
        """
        if not user_ids:
            return {}
        
        session = await self.get_session()
        headers = await self.get_headers()
        
        url = f"{self.base_url}/users"
        params = [('id', user_id) for user_id in user_ids[:100]]
        
        try:
            async with session.get(url, headers=headers, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    return {
                        user['id']: user['profile_image_url'] 
                        for user in data['data']
                    }
                else:
                    return {}
        except Exception as e:
            logger.error(f"Exception getting profile images: {e}")
            return {}
