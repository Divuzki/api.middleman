from django.urls import re_path
from .views import DepositView, WithdrawalView, TransactionListView, VerifyDepositView, PaymentSelectionPage, ProcessPaymentChoice, KorapayWebhookView, NOWPaymentsWebhookView

urlpatterns = [
    re_path(r'^users/deposit/?$', DepositView.as_view(), name='deposit'),
    re_path(r'^users/deposit/select/(?P<reference>\w+)/?$', PaymentSelectionPage.as_view(), name='payment-selection'),
    re_path(r'^users/deposit/process/(?P<reference>\w+)/?$', ProcessPaymentChoice.as_view(), name='process-payment-choice'),
    re_path(r'^users/deposit/verify/(?P<reference>\w+)/?$', VerifyDepositView.as_view(), name='verify-deposit'),
    re_path(r'^user/withdraw/?$', WithdrawalView.as_view(), name='withdraw'),
    re_path(r'^transactions/?$', TransactionListView.as_view(), name='transactions'),
    re_path(r'^webhooks/korapay/?$', KorapayWebhookView.as_view(), name='korapay-webhook'),
    re_path(r'^webhooks/nowpayments/?$', NOWPaymentsWebhookView.as_view(), name='nowpayments-webhook'),
]
