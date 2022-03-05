import json
import logging
from copy import deepcopy

from django.conf import settings
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.module_loading import import_string
from django.utils.translation import gettext as _
from django.views import View
from spid_cie_oidc.entity.exceptions import InvalidTrustchain
from spid_cie_oidc.entity.jwtse import (
    create_jws,
    unpad_jwt_head,
    unpad_jwt_payload,
    verify_jws,
)
from spid_cie_oidc.entity.models import FederationEntityConfiguration, TrustChain
from spid_cie_oidc.entity.settings import HTTPC_PARAMS
from spid_cie_oidc.entity.statements import get_http_url
from spid_cie_oidc.entity.trust_chain_operations import get_or_create_trust_chain
from spid_cie_oidc.onboarding.schemas.authn_requests import AcrValuesSpid

from .models import OidcAuthentication, OidcAuthenticationToken
from .oauth2 import *
from .oidc import *
from .settings import (
    RP_ATTR_MAP,
    RP_PKCE_CONF,
    RP_REQUEST_CLAIM_BY_PROFILE,
    RP_USER_CREATE,
    RP_USER_LOOKUP_FIELD,
)
from .utils import (
    http_dict_to_redirect_uri_path,
    http_redirect_uri_to_dict,
    process_user_attributes,
    random_string,
)

logger = logging.getLogger(__name__)


class SpidCieOidcRp:
    """
    Baseclass with common methods for RPs
    """

    def get_jwks_from_jwks_uri(self, jwks_uri: str) -> dict:
        """
        get jwks
        """
        try:
            jwks_dict = get_http_url([jwks_uri], httpc_params=HTTPC_PARAMS).json()
        except Exception as e:
            logger.error(f"Failed to download jwks from {jwks_uri}: {e}")
            return {}
        return jwks_dict

    def get_oidc_op(self, request) -> TrustChain:
        """
        get available trust to a specific OP
        """
        if not request.GET.get("provider", None):
            logger.warning(
                "Missing provider url. Please try '?provider=https://provider-subject/'"
            )
            raise InvalidTrustchain(
                "Missing provider url. Please try '?provider=https://provider-subject/'"
            )

        trust_anchor = request.GET.get("trust_anchor", settings.OIDCFED_TRUST_ANCHOR)
        if trust_anchor not in settings.OIDCFED_TRUST_ANCHORS:
            logger.warning("Unallowed Trust Anchor")
            raise InvalidTrustchain("Unallowed Trust Anchor")

        tc = TrustChain.objects.filter(
            sub=request.GET["provider"],
            trust_anchor__sub=trust_anchor,
        ).first()
        if not tc:
            logger.info(f'Trust Chain not found for {request.GET["provider"]}')
        elif not tc.is_active:
            logger.warning(f"{tc} found but DISABLED at {tc.modified}")
            raise InvalidTrustchain(f"{tc} found but DISABLED at {tc.modified}")
        elif tc.is_expired:
            logger.warning(f"{tc} found but expired at {tc.exp}")
            logger.warning("We should try to renew the trust chain")
            tc = get_or_create_trust_chain(
                subject=tc.sub,
                trust_anchor=trust_anchor,
                # TODO
                # required_trust_marks: list = [],
                metadata_type="openid_provider",
                force=True,
            )
        return tc


class SpidCieOidcRpBeginView(SpidCieOidcRp, View):
    """View which processes the actual Authz request and
    returns a Http Redirect
    """

    error_template = "rp_error.html"

    def get(self, request, *args, **kwargs):
        """
        http://localhost:8001/oidc/rp/authorization?
        provider=http://127.0.0.1:8002/
        """
        try:
            tc = self.get_oidc_op(request)
        except InvalidTrustchain as exc:
            context = {
                "error": _("Request rejected"),
                "error_description": _(str(exc.args)),
            }
            return render(request, self.error_template, context)

        if not tc:
            context = {
                "error": _("Request rejected"),
                "error_description": "Trust Chain is unavailable.",
            }
            return render(request, self.error_template, context)
        provider_metadata = tc.metadata
        if not provider_metadata:
            context = {
                "error": _("Request rejected"),
                "error_description": _("provider metadata not found"),
            }
            return render(request, self.error_template, context)

        entity_conf = FederationEntityConfiguration.objects.filter(
            entity_type="openid_relying_party",
            # TODO: RPs multitenancy?
            # sub = request.build_absolute_uri()
        ).first()
        if not entity_conf:
            context = {
                "error": _("Request rejected"),
                "error_description": _("Missing configuration."),
            }
            return render(request, self.error_template, context)

        client_conf = entity_conf.metadata["openid_relying_party"]
        if not (
            provider_metadata.get("jwks_uri", None)
            or provider_metadata.get("jwks", None)
        ):
            context = {
                "error": _("Request rejected"),
                "error_description": _("Invalid provider Metadata."),
            }
            return render(request, self.error_template, context)

        if provider_metadata.get("jwks", None):
            jwks_dict = provider_metadata["jwks"]
        else:
            jwks_dict = self.get_jwks_from_jwks_uri(provider_metadata["jwks_uri"])
        if not jwks_dict:
            _msg = f"Failed to get jwks from {tc.sub}"
            logger.error(f"{_msg}:")
            context = {
                "error": _("Request rejected"),
                "error_description": _(f"{_msg}:"),
            }
            return render(request, self.error_template, context)

        authz_endpoint = provider_metadata["authorization_endpoint"]

        redirect_uri = request.GET.get("redirect_uri", client_conf["redirect_uris"][0])
        if redirect_uri not in client_conf["redirect_uris"]:
            redirect_uri = client_conf["redirect_uris"][0]

        authz_data = dict(
            scope=" ".join([i for i in request.GET.get("scope", ["openid"])]),
            redirect_uri=redirect_uri,
            response_type=client_conf["response_types"][0],
            nonce=random_string(32),
            state=random_string(32),
            client_id=client_conf["client_id"],
            endpoint=authz_endpoint,
            acr_values=request.GET.get("acr_values", AcrValuesSpid.l2.value),
            iat=int(timezone.localtime().timestamp()),
            aud=[tc.sub, authz_endpoint],
            claims=RP_REQUEST_CLAIM_BY_PROFILE[request.GET.get("profile", "spid")],
        )

        _prompt = request.GET.get("prompt", "consent login")

        # if "offline_access" in authz_data["scope"]:
        # _prompt.extend(["consent login"])

        authz_data["prompt"] = _prompt

        # PKCE
        pkce_func = import_string(RP_PKCE_CONF["function"])
        pkce_values = pkce_func(**RP_PKCE_CONF["kwargs"])
        authz_data.update(pkce_values)
        #

        authz_entry = dict(
            client_id=client_conf["client_id"],
            state=authz_data["state"],
            endpoint=authz_endpoint,
            # TODO: better have here an organization name
            provider=tc.sub,
            provider_id=tc.sub,
            data=json.dumps(authz_data),
            provider_jwks=json.dumps(jwks_dict),
            provider_configuration=json.dumps(provider_metadata),
        )

        # TODO: Prune the old or unbounded authz ...
        OidcAuthentication.objects.create(**authz_entry)

        authz_data.pop("code_verifier")
        # add the signed request object
        authz_data_obj = deepcopy(authz_data)
        authz_data_obj["iss"] = client_conf["client_id"]
        authz_data_obj["sub"] = client_conf["client_id"]
        request_obj = create_jws(authz_data_obj, entity_conf.jwks[0])
        authz_data["request"] = request_obj
        uri_path = http_dict_to_redirect_uri_path(authz_data)
        url = "?".join((authz_endpoint, uri_path))
        http_redirect_uri_to_dict(url)
        logger.info(f"Starting Authz request to {url}")
        return HttpResponseRedirect(url)


class SpidCieOidcRpCallbackView(View, OidcUserInfo, OAuth2AuthorizationCodeGrant):
    """
    View which processes an Authorization Response
    https://tools.ietf.org/html/rfc6749#section-4.1.2

    eg:
    /redirect_uri?code=tYkP854StRqBVcW4Kg4sQfEN5Qz&state=R9EVqaazGsj3wg5JgxIgm8e8U4BMvf7W


    """

    error_template = "rp_error.html"

    def user_reunification(self, user_attrs: dict, client_conf: dict):
        user_model = get_user_model()
        lookup = {
            f"attributes__{RP_USER_LOOKUP_FIELD}": user_attrs[RP_USER_LOOKUP_FIELD]
        }
        user = user_model.objects.filter(**lookup).first()
        if user:
            user.attributes.update(user_attrs)
            user.save()
            logger.info(f"{RP_USER_LOOKUP_FIELD} matched on user {user}")
            return user
        elif RP_USER_CREATE:
            user = user_model.objects.create(
                username=user_attrs.get("username", user_attrs["sub"]),
                first_name=user_attrs.get("given_name", user_attrs["sub"]),
                surname=user_attrs.get("family_name", user_attrs["sub"]),
                email=user_attrs.get("email", ""),
                attributes=user_attrs,
            )
            logger.info(f"Created new user {user}")
            return user

    def get_jwk_from_jwt(self, jwt: str, provider_jwks: dict) -> dict:
        head = unpad_jwt_head(jwt)
        kid = head["kid"]
        for jwk in provider_jwks:
            if jwk["kid"] == kid:
                return jwk
        return {}

    def get(self, request, *args, **kwargs):
        """
        docs here
        """
        request_args = {k: v for k, v in request.GET.items()}
        if "error" in request_args:
            return render(request, self.error_template, request_args, status=401)

        authz = OidcAuthentication.objects.filter(
            state=request_args.get("state"),
        )
        if not authz:
            # TODO: verify error message and status
            context = {
                "error": _("Request unauthorized"),
                "error_description": _("Authentication not found"),
            }
            return render(request, self.error_template, context, status=401)
        else:
            authz = authz.last()

        authz_data = json.loads(authz.data)
        provider_conf = authz.get_provider_configuration()

        code = request.GET.get("code")
        if not code:
            # TODO: verify error message and status
            context = {
                "error": _("Invalid request"),
                "error_description": _("Request MUST contain code"),
            }
            return render(request, self.error_template, context, status=400)

        authz_token = OidcAuthenticationToken.objects.create(
            authz_request=authz, code=code
        )
        self.rp_conf = FederationEntityConfiguration.objects.get(
            sub=authz_token.authz_request.client_id
        )

        if not self.rp_conf:
            # TODO: verify error message and status
            context = {
                "error": _("Invalid request"),
                "error_description": _("Relay party not found"),
            }
            return render(request, self.error_template, context, status=400)

        client_conf = self.rp_conf.metadata["openid_relying_party"]
        token_response = self.access_token_request(
            redirect_uri=authz_data["redirect_uri"],
            state=authz.state,
            code=code,
            issuer_id=authz.provider_id,
            client_conf=self.rp_conf,
            token_endpoint_url=provider_conf["token_endpoint"],
            audience=[authz.provider_id],
            code_verifier=authz_data.get("code_verifier"),
        )
        if not token_response:
            # TODO: verify error message
            context = {
                "error": _("Not valid token response"),
                "error_description": _("Token response seems not to be valid"),
            }
            return render(request, self.error_template, context, status=400)
        # da verificare
        entity_conf = FederationEntityConfiguration.objects.filter(
            entity_type="openid_provider",
        ).first()

        op_conf = entity_conf.metadata["openid_provider"]
        jwks = op_conf["jwks"]["keys"]
        ###################
        access_token = token_response["access_token"]
        id_token = token_response["id_token"]
        op_ac_jwk = self.get_jwk_from_jwt(access_token, jwks)
        op_id_jwk = self.get_jwk_from_jwt(id_token, jwks)
        if not op_ac_jwk or not op_id_jwk:
            # TODO: verify error message and status
            context = {
                "error": _("Not valid authentication token"),
                "error_description": _("Authentication token seems not to be valid."),
            }
            return render(request, self.error_template, context, status=403)
        if not verify_jws(access_token, op_ac_jwk):
            pass
            # Actually AgID Login have a non-JWT access token!
            # return HttpResponseBadRequest(
            # _('Authentication response validation error.')
            # )
        if not verify_jws(access_token, op_id_jwk):
            # TODO: verify error message
            context = {
                "error": _("Not valid authentication token"),
                "error_description": _("Authentication token validation error."),
            }
            return render(request, self.error_template, context, status=403)

        # just for debugging purpose ...
        # sost
        decoded_id_token = unpad_jwt_payload(id_token)
        logger.debug(decoded_id_token)
        decoded_access_token = unpad_jwt_payload(access_token)
        logger.debug(decoded_access_token)

        authz_token.access_token = access_token
        authz_token.id_token = id_token
        authz_token.scope = token_response.get("scope")
        authz_token.token_type = token_response["token_type"]
        authz_token.expires_in = token_response["expires_in"]
        authz_token.save()
        userinfo = self.get_userinfo(
            authz.state,
            authz_token.access_token,
            provider_conf,
            verify=HTTPC_PARAMS,
        )
        if not userinfo:
            # TODO: verify error message
            context = {
                "error": _("Not valid UserInfo response"),
                "error_description": _("UserInfo response seems not to be valid"),
            }
            return render(request, self.error_template, context, status=400)

        # here django user attr mapping
        user_attrs = process_user_attributes(userinfo, RP_ATTR_MAP, authz.__dict__)
        if not user_attrs:
            _msg = "No user attributes have been processed"
            logger.warning(f"{_msg}: {userinfo}")
            # TODO: verify error message and status
            context = {
                "error": _("No user attributes have been processed"),
                "error_description": _(f"{_msg}: {userinfo}"),
            }
            return render(request, self.error_template, context, status=403)

        user = self.user_reunification(user_attrs, client_conf)
        if not user:
            # TODO: verify error message and status
            context = {"error": _("No user found"), "error_description": _("")}
            return render(request, self.error_template, context, status=403)

        # authenticate the user
        login(request, user)
        request.session["oidc_rp_user_attrs"] = user_attrs
        authz_token.user = user
        authz_token.save()

        return HttpResponseRedirect(
            getattr(
                settings, "LOGIN_REDIRECT_URL", reverse("spid_cie_rp_echo_attributes")
            )
        )


class SpidCieOidcRpCallbackEchoAttributes(View):
    template = "echo_attributes.html"

    def get(self, request):
        data = {"oidc_rp_user_attrs": request.session["oidc_rp_user_attrs"]}
        return render(request, self.template, data)


@login_required
def oidc_rpinitiated_logout(request):
    """
    http://localhost:8000/end-session/?id_token_hint=
    """
    auth_tokens = OidcAuthenticationToken.objects.filter(user=request.user).filter(
        Q(logged_out__iexact="") | Q(logged_out__isnull=True)
    )
    authz = auth_tokens.last().authz_request
    provider_conf = authz.get_provider_configuration()
    end_session_url = provider_conf.get("end_session_endpoint")

    # first of all on RP side ...
    logout(request)

    if not end_session_url:
        logger.warning(f"{authz.issuer_url} does not support end_session_endpoint !")
        return HttpResponseRedirect(settings.LOGOUT_REDIRECT_URL)
    else:
        auth_token = auth_tokens.last()
        url = f"{end_session_url}?id_token_hint={auth_token.id_token}"
        auth_token.logged_out = timezone.localtime()
        auth_token.save()
        return HttpResponseRedirect(url)


def oidc_rp_landing(request):
    trust_chains = TrustChain.objects.filter(type="openid_provider")
    providers = []
    for tc in trust_chains:
        if tc.is_valid:
            providers.append(tc)
    content = {"providers": providers}
    return render(request, "rp_landing.html", content)
