from django.urls import re_path
from .views import DepositView, WithdrawalView, TransactionListView, VerifyDepositView, PaystackWebhookView, NOWPaymentsWebhookView

urlpatterns = [
    re_path(r'^users/deposit/?$', DepositView.as_view(), name='deposit'),
    re_path(r'^users/deposit/verify/(?P<reference>\w+)/?$', VerifyDepositView.as_view(), name='verify-deposit'),
    re_path(r'^user/withdraw/?$', WithdrawalView.as_view(), name='withdraw'),
    re_path(r'^transactions/?$', TransactionListView.as_view(), name='transactions'),
    re_path(r'^webhooks/paystack/?$', PaystackWebhookView.as_view(), name='paystack-webhook'),
    re_path(r'^webhooks/nowpayments/?$', NOWPaymentsWebhookView.as_view(), name='nowpayments-webhook'),
]
