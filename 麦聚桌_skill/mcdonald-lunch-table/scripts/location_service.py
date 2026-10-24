"""User-initiated browser coordinates -> city/landmark -> official store search."""
from __future__ import annotations

import math
import threading
import time

import httpx
from mcd_adapter import AdapterError


class LocationResolver:
    def __init__(self, client=None):
        self.client = client or httpx.Client(timeout=15, follow_redirects=False)
        self.lock = threading.Lock()
        self.last_request = 0.0
        self.cache = {}

    def resolve(self, latitude, longitude):
        if (isinstance(latitude, bool) or isinstance(longitude, bool) or
                not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)) or
                not math.isfinite(latitude) or not math.isfinite(longitude) or
                not -90 <= latitude <= 90 or not -180 <= longitude <= 180):
            raise AdapterError('location_invalid', '定位坐标无效，请重新授权定位')
        key = (round(latitude, 3), round(longitude, 3))
        with self.lock:
            cached = self.cache.get(key)
            if cached and time.monotonic() - cached[0] < 600:
                return dict(cached[1])
            # Public, user-initiated reverse lookup only; no autocomplete or bulk scans.
            result = None
            providers = [
                ('photon', 'https://photon.komoot.io/reverse', {'lat': latitude, 'lon': longitude}),
                ('nominatim', 'https://nominatim.openstreetmap.org/reverse',
                 {'lat': latitude, 'lon': longitude, 'format': 'jsonv2', 'zoom': 18, 'accept-language': 'zh-CN'}),
            ]
            for name, url, params in providers:
                time.sleep(max(0, 1.1 - (time.monotonic() - self.last_request)))
                self.last_request = time.monotonic()
                try:
                    response = self.client.get(url, params=params, timeout=7,
                        headers={'User-Agent': 'Maijuzhuo/0.4 (user-initiated local restaurant assistant)'})
                    if response.status_code != 200:
                        raise ValueError
                    data = response.json()
                    if name == 'photon':
                        address = data['features'][0]['properties']
                        city = address.get('city') or address.get('county')
                        state = address.get('state', '')
                        if any(place in state for place in ('上海', '北京', '天津', '重庆')):
                            city = state
                        keyword = address.get('street') or address.get('locality') or address.get('district') or address.get('name')
                    else:
                        address = data.get('address', {})
                        city = address.get('city') or address.get('town') or address.get('municipality') or address.get('state')
                        keyword = address.get('road') or address.get('neighbourhood') or address.get('suburb') or address.get('quarter') or data.get('name')
                    if not isinstance(city, str) or not isinstance(keyword, str) or not city or not keyword:
                        raise ValueError
                    result = {'city': city, 'keyword': keyword, 'label': city + keyword,
                              'attribution': '© OpenStreetMap contributors / ' + name.title(),
                              'source': 'browser_geolocation'}
                    break
                except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError):
                    continue
            if result is None:
                raise AdapterError('location_unavailable', '已获得坐标，但地址解析暂不可用。可以直接告诉我当前门店名继续点餐') from None
            if len(self.cache) > 100:
                self.cache.clear()
            self.cache[key] = (time.monotonic(), result)
            return dict(result)
