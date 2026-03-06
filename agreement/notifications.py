from users.notifications import send_status_notification

def send_agreement_notification(agreement, status=None):
    """
    Sends push notifications to agreement participants about status changes.
    
    Args:
        agreement: The Agreement instance
        status: Optional status string. If not provided, uses agreement.status
    """
    if not status:
        status = agreement.status

    # Determine participants
    participants = set()
    if agreement.initiator:
        participants.add(agreement.initiator)
    if agreement.counterparty:
        participants.add(agreement.counterparty)
    
    # Buyer/Seller are usually one of the above, but add just in case
    if agreement.buyer:
        participants.add(agreement.buyer)
    if agreement.seller:
        participants.add(agreement.seller)

    # Determine message content based on status
    title = "Agreement Update"
    body = f"Status update for agreement: {agreement.title}"

    if status == 'draft':
        return # No notification for draft
    elif status == 'awaiting_acceptance':
        title = "New Agreement Offer"
        body = f"You have received a new agreement offer: {agreement.title}"
    elif status == 'terms_locked':
        title = "Terms Locked"
        body = f"The terms for agreement '{agreement.title}' have been locked."
    elif status == 'active':
        title = "Agreement Active"
        body = f"Agreement '{agreement.title}' is now active."
    elif status == 'secured':
        title = "Payment Secured"
        body = f"Payment for '{agreement.title}' has been secured in escrow."
    elif status == 'delivered':
        title = "Service Delivered"
        body = f"Service/Item for '{agreement.title}' has been marked as delivered."
    elif status == 'completed':
        title = "Agreement Completed"
        body = f"Agreement '{agreement.title}' has been successfully completed."
    elif status == 'disputed':
        title = "Agreement Disputed"
        body = f"A dispute has been raised for agreement '{agreement.title}'."
    elif status == 'cancelled':
        title = "Agreement Cancelled"
        body = f"Agreement '{agreement.title}' has been cancelled."
    
    # Send to each participant
    for user in participants:
        send_status_notification(
            recipient=user,
            title=title,
            body=body,
            conversation_id=agreement.id,
            conversation_type='agreement',
            status=status
        )
