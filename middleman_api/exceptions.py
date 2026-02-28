from rest_framework.views import exception_handler
from rest_framework.response import Response
from rest_framework import status

def custom_exception_handler(exc, context):
    # Call REST framework's default exception handler first,
    # to get the standard error response.
    response = exception_handler(exc, context)

    if response is not None:
        custom_data = {
            "status": "error",
            "code": "error",
            "message": "An error occurred."
        }

        if isinstance(response.data, dict):
            if 'detail' in response.data:
                custom_data['message'] = str(response.data['detail'])
                custom_data['code'] = str(response.data.get('code', 'error'))
            else:
                custom_data['message'] = "Validation error"
                custom_data['code'] = "validation_error"
                custom_data['errors'] = response.data
        elif isinstance(response.data, list):
             custom_data['message'] = str(response.data)
        else:
            custom_data['message'] = str(response.data)

        response.data = custom_data

    return response
