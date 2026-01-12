from django.urls import path
from .views import DepositView, WithdrawalView, TransactionListView, VerifyDepositView

urlpatterns = [
    path('users/deposit', DepositView.as_view(), name='deposit'),
    path('users/deposit/verify/<str:reference>', VerifyDepositView.as_view(), name='verify-deposit'),
    path('user/withdraw', WithdrawalView.as_view(), name='withdraw'),
    path('transactions', TransactionListView.as_view(), name='transactions'),
]
