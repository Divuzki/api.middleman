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

def send_otp_email(user, otp):
    """
    Sends an OTP email for PIN change.
    """
    subject = "Your Verification Code"
    from_email = settings.DEFAULT_FROM_EMAIL
    to = [user.email]

    # Simple text content for now, ideally use a template
    text_content = f"Your verification code is: {otp}. It expires in 10 minutes."
    
    # HTML content
    html_content = f"""
    <div style="font-family: Arial, sans-serif; padding: 20px;">
        <h2>Verification Code</h2>
        <p>Use the code below to authorize your request:</p>
        <h1 style="color: #4CAF50; letter-spacing: 5px;">{otp}</h1>
        <p>This code expires in 10 minutes.</p>
        <p>If you did not request this, please ignore this email.</p>
    </div>
    """

    try:
        msg = EmailMultiAlternatives(subject, text_content, from_email, to)
        msg.attach_alternative(html_content, "text/html")
        msg.send()
        return True
    except Exception as e:
        print(f"Failed to send OTP email to {user.email}: {str(e)}")
        return False
