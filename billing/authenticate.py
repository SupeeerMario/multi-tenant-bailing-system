from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication, get_authorization_header

from .models import Tenant


class TenantAuthentication(BaseAuthentication):

    keyword = 'Api-Key'

    def authenticate(self, request):

        auth  = get_authorization_header(request).split()

        if not auth or auth[0].lower() != self.keyword.lower().encode():
            return None

        if len(auth) == 1:
            msg = 'Invalid token header. No credentials provided.'
            raise exceptions.AuthenticationFailed(msg)
        elif len(auth) > 2:
            msg = 'Invalid token header. Token string should not contain spaces.'
            raise exceptions.AuthenticationFailed(msg)


        try:
            token = auth[1].decode()

        except UnicodeError:
            msg = 'Wrong Key'
            raise exceptions.AuthenticationFailed(msg)


        try:
            tenant = Tenant.objects.get(api_key = token)

        except Tenant.DoesNotExist:
            msg = 'Tenant not found'
            raise exceptions.AuthenticationFailed(msg)

        if not tenant.is_active:
            msg = 'Tenant not found'
            raise exceptions.AuthenticationFailed(msg)

        request.tenant = tenant
        return (tenant, None)


    def authenticate_header(self,request):
        return self.keyword


class TenantAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = 'billing.authenticate.TenantAuthentication'
    name = 'ApiKeyAuth'

    def get_security_definition(self, auto_schema):
        return {
            'type': 'apiKey',
            'in': 'header',
            'name': 'Authorization',
            'description': 'Tenant API key. Paste the whole value including the prefix: Api-Key <your key>',
        }