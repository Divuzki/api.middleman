from rest_framework.views import exception_handler
from rest_framework.response import Response
from rest_framework import status
from rest_framework.exceptions import APIException

class GatewayError(APIException):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = 'Bad Gateway'
    default_code = 'bad_gateway'

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
            elif 'message' in response.data:
                custom_data['message'] = str(response.data['message'])
            elif 'error' in response.data:
                custom_data['message'] = str(response.data['error'])
            elif 'non_field_errors' in response.data:
                errors = response.data['non_field_errors']
                if isinstance(errors, list) and len(errors) > 0:
                    custom_data['message'] = str(errors[0])
                else:
                    custom_data['message'] = str(errors)
            else:
                # If there are field-specific errors, try to grab the first one for the message
                # or just say "Validation error"
                first_error = next(iter(response.data.values()), None)
                if isinstance(first_error, list) and len(first_error) > 0:
                     custom_data['message'] = str(first_error[0])
                elif isinstance(first_error, str):
                     custom_data['message'] = first_error
                else:
                     custom_data['message'] = "Validation error"
                
                custom_data['code'] = "validation_error"
                # Put the full error details in 'data'
                custom_data['data'] = response.data

        elif isinstance(response.data, list):
             if len(response.data) > 0:
                 custom_data['message'] = str(response.data[0])
             else:
                 custom_data['message'] = "Validation error"
        else:
            custom_data['message'] = str(response.data)

        response.data = custom_data

    return response
