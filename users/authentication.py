from rest_framework import authentication
from rest_framework import exceptions
from firebase_admin import auth
from django.contrib.auth import get_user_model
from django.conf import settings
import firebase_admin
from .emails import send_welcome_email

User = get_user_model()

class FirebaseAuthentication(authentication.BaseAuthentication):
    def authenticate(self, request):
        auth_header = request.META.get('HTTP_AUTHORIZATION')
        if not auth_header:
            return None

        id_token = auth_header.split(' ').pop()
        
        try:
            decoded_token = auth.verify_id_token(id_token)
        except Exception as e:
            raise exceptions.AuthenticationFailed(f'Invalid token: {str(e)}')

        uid = decoded_token.get('uid')
        email = decoded_token.get('email')
        
        if not email:
            raise exceptions.AuthenticationFailed('Token missing email')

        # Get or create user
        try:
            user = User.objects.get(email=email)
            # Update firebase_uid if missing or changed (though email is unique)
            if user.firebase_uid != uid:
                user.firebase_uid = uid
                user.save()
        except User.DoesNotExist:
            # Create new user
            user_data = {
                'email': email,
                'firebase_uid': uid,
                'first_name': decoded_token.get('name', '').split(' ')[0] if decoded_token.get('name') else '',
                'last_name': ' '.join(decoded_token.get('name', '').split(' ')[1:]) if decoded_token.get('name') else '',
                'image_url': decoded_token.get('picture'),
            }
            # We need to set a password for Django user, even if unusable
            user = User.objects.create_user(**user_data)
            user.set_unusable_password()
            user.save()

            # Send welcome email
            send_welcome_email(user)


        return (user, None)
