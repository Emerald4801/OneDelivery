"""Shared failure classification. Unknown failures require operator intervention."""
import re

import requests

STARTUP_RETRY = 60
CYCLE_RETRY = 300


def temporary_http(status):
    return type(status) is int and (status in (408, 425, 429) or 500 <= status <= 599)


def should_retry(error):
    declared = getattr(error, 'retryable', None)
    if type(declared) is bool:
        return declared
    if isinstance(error, requests.exceptions.SSLError):
        return False
    if isinstance(error, (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError)):
        return True
    if isinstance(error, requests.HTTPError):
        response = error.response
        return temporary_http(response.status_code if response is not None else None)
    return False


def failure_message(error):
    if isinstance(error, requests.exceptions.SSLError):
        return 'Не удалось проверить защищённое соединение. Проверьте сеть и сертификаты.'
    if isinstance(error, (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError)):
        return 'Нет соединения с сервисом. Прогресс сохранён для повторной попытки.'
    if isinstance(error, requests.HTTPError):
        response = error.response
        status = response.status_code if response is not None else 'неизвестно'
        return f'Сервис отклонил запрос: HTTP {status}. Прогресс сохранён.'
    if isinstance(error, OSError):
        return 'Не удалось прочитать или записать локальные данные. Проверьте доступ к рабочей папке и свободное место.'
    if isinstance(error, (RuntimeError, ValueError)):
        return re.sub(r'https?://\S+', '[адрес скрыт]', str(error))[:1500]
    return f'Внутренняя ошибка ({type(error).__name__}). Прогресс сохранён; требуется проверка.'
