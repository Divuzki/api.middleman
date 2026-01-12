from django.urls import path
from .views import (
    AuthView, UserProfileUpdateView, UserProfilePictureUpdateView,
    BankListView, PayoutAccountListCreateView, PayoutAccountDeleteView,
    VerifyBankAccountView, IdentityVerificationView, IdentityStatusView,
    SetAccountPinView
)

urlpatterns = [
    path('auth/', AuthView.as_view(), name='auth'),
    path('user/profile', UserProfileUpdateView.as_view(), name='update-profile'),
    path('user/profile-picture', UserProfilePictureUpdateView.as_view(), name='update-profile-picture'),
    path('banks', BankListView.as_view(), name='bank-list'),
    path('user/payout-accounts', PayoutAccountListCreateView.as_view(), name='payout-account-list-create'),
    path('user/payout-accounts/verify', VerifyBankAccountView.as_view(), name='verify-bank-account'),
    path('user/payout-accounts/<int:id>', PayoutAccountDeleteView.as_view(), name='delete-payout-account'),
    path('user/verify-identity', IdentityVerificationView.as_view(), name='verify-identity'),
    path('user/identity-status', IdentityStatusView.as_view(), name='identity-status'),
    path('user/pin', SetAccountPinView.as_view(), name='set-account-pin'),
]
