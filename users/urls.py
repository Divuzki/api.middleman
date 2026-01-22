from django.urls import path, re_path
from .views import (
    AuthView, UserProfileUpdateView, UserProfilePictureUpdateView,
    BankListView, PayoutAccountListCreateView, PayoutAccountDeleteView,
    VerifyBankAccountView, IdentityVerificationView, IdentityStatusView,
    SetAccountPinView, DeviceListCreateView, DeviceDetailView
)

urlpatterns = [
    re_path(r'^auth/?$', AuthView.as_view(), name='auth'),
    re_path(r'^user/profile/?$', UserProfileUpdateView.as_view(), name='update-profile'),
    re_path(r'^user/profile-picture/?$', UserProfilePictureUpdateView.as_view(), name='update-profile-picture'),
    re_path(r'^banks/?$', BankListView.as_view(), name='bank-list'),
    re_path(r'^user/payout-accounts/?$', PayoutAccountListCreateView.as_view(), name='payout-account-list-create'),
    re_path(r'^user/payout-accounts/verify/?$', VerifyBankAccountView.as_view(), name='verify-bank-account'),
    re_path(r'^user/payout-accounts/(?P<id>\d+)/?$', PayoutAccountDeleteView.as_view(), name='delete-payout-account'),
    re_path(r'^user/verify-identity/?$', IdentityVerificationView.as_view(), name='verify-identity'),
    re_path(r'^user/identity-status/?$', IdentityStatusView.as_view(), name='identity-status'),
    re_path(r'^user/pin/?$', SetAccountPinView.as_view(), name='set-account-pin'),
    re_path(r'^user/devices/?$', DeviceListCreateView.as_view(), name='device-list-create'),
    re_path(r'^user/devices/(?P<device_uuid>[0-9a-f-]+)/?$', DeviceDetailView.as_view(), name='device-detail'),
]
