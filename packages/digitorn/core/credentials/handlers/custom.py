"""CustomHandler — catch-all for any provider type not covered built-in.

When an app declares a credential with a novel ``type: xyz`` and no
handler is registered for it, the registry falls back to this class.
It implements the minimum contract (validation via schema + inert
test/refresh/revoke) so unknown types don't break the daemon.

Apps wanting more than the default can register their own handler
via the plugin surface — but for the 80% case of "I just need to
store 3 fields in an encrypted blob", this catch-all is enough.
"""

from __future__ import annotations

from digitorn.core.credentials.handler import CredentialHandler


class CustomHandler(CredentialHandler):
    provider_type = "custom"
