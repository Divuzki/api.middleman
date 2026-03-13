import logging

logger = logging.getLogger(__name__)

class RequestLoggingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Log request headers
        origin = request.headers.get('Origin', 'No Origin')
        host = request.headers.get('Host', 'No Host')
        user_agent = request.headers.get('User-Agent', 'No User-Agent')
        method = request.method
        path = request.path
        
        # print(f"Request: {method} {path} - Origin: {origin}")
        # print(f"Request Host: {host}")
        # print(f"Request User-Agent: {user_agent}")
        
        # Also log to standard logger if configured
        logger.info(f"Incoming Request - Method: {method}, Path: {path}, Origin: {origin}, Host: {host}, User-Agent: {user_agent}")

        response = self.get_response(request)
        
        # Log response status
        # print(f"Response Status: {response.status_code}")
        logger.info(f"Response Status: {response.status_code}")
        
        return response
