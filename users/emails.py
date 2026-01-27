from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.conf import settings
from django.utils.html import strip_tags

def send_welcome_email(user):
    """
    Sends a welcome email to a new user.
    """
    subject = "Welcome to Middleman!"
    from_email = settings.DEFAULT_FROM_EMAIL
    to = [user.email]

    # Context for the template
    context = {
        'first_name': user.first_name if user.first_name else user.email.split('@')[0],
        'email': user.email,
    }

    # Render HTML content
    html_content = render_to_string('emails/welcome_email.html', context)
    
    # Create text fallback
    text_content = strip_tags(html_content)

    try:
        msg = EmailMultiAlternatives(subject, text_content, from_email, to)
        msg.attach_alternative(html_content, "text/html")
        msg.send()
        return True
    except Exception as e:
        # Log the error in a real production environment
        print(f"Failed to send welcome email to {user.email}: {str(e)}")
        return False
