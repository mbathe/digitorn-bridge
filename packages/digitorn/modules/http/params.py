"""Pydantic parameter models for the HTTP module."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RequestParams(BaseModel):
    """Make an HTTP request with full control over method, headers, body.

    Examples:
        request(url="https://api.example.com/users", method="GET")
        request(url="https://api.example.com/users", method="POST", json_body={"name": "Alice"})
        request(url="https://api.example.com/data", method="GET", headers={"Authorization": "Bearer token123"})
    """
    url: str = Field(..., description="Target URL (http:// or https://).")
    method: str = Field("GET", description="HTTP method: GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS.")
    headers: dict[str, str] | None = Field(None, description="Custom request headers.")
    body: str | None = Field(None, description="Raw request body.")
    json_body: dict | list | None = Field(None, description="JSON body (auto-sets Content-Type: application/json).")
    query_params: dict[str, str] | None = Field(None, description="URL query parameters.")
    timeout: float = Field(30.0, ge=1.0, le=300.0, description="Request timeout in seconds.")
    follow_redirects: bool = Field(True, description="Follow HTTP redirects.")
    max_redirects: int = Field(10, ge=0, le=20, description="Maximum redirect hops.")
    verify_tls: bool = Field(True, description="Verify TLS certificates.")
    max_response_bytes: int = Field(5_000_000, ge=1, le=50_000_000, description="Max response body to read (default 5 MB).")


class GetParams(BaseModel):
    """HTTP GET - fetch a URL and auto-parse the response.

    Examples:
        get(url="https://api.example.com/users")
        get(url="https://api.example.com/users/42", headers={"Authorization": "Bearer token"})
    """
    url: str = Field(..., description="Target URL.")
    headers: dict[str, str] | None = Field(None, description="Custom request headers.")
    query_params: dict[str, str] | None = Field(None, description="URL query parameters.")
    timeout: float = Field(30.0, ge=1.0, le=300.0, description="Request timeout in seconds.")
    verify_tls: bool = Field(True, description="Verify TLS certificates.")
    max_response_bytes: int = Field(5_000_000, ge=1, le=50_000_000, description="Max response body to read.")


class PostParams(BaseModel):
    """HTTP POST - send data to a URL.

    Examples:
        post(url="https://api.example.com/users", json_body={"name": "Alice", "email": "alice@example.com"})
        post(url="https://api.example.com/upload", body="raw data here")
    """
    url: str = Field(..., description="Target URL.")
    headers: dict[str, str] | None = Field(None, description="Custom request headers.")
    json_body: dict | list | None = Field(None, description="JSON payload (auto-sets Content-Type).")
    body: str | None = Field(None, description="Raw body (mutually exclusive with json_body).")
    timeout: float = Field(30.0, ge=1.0, le=300.0, description="Request timeout in seconds.")
    verify_tls: bool = Field(True, description="Verify TLS certificates.")


class PutParams(BaseModel):
    """HTTP PUT - replace a resource."""
    url: str = Field(..., description="Target URL.")
    headers: dict[str, str] | None = Field(None, description="Custom request headers.")
    json_body: dict | list | None = Field(None, description="JSON payload.")
    body: str | None = Field(None, description="Raw body.")
    timeout: float = Field(30.0, ge=1.0, le=300.0, description="Request timeout in seconds.")
    verify_tls: bool = Field(True, description="Verify TLS certificates.")


class PatchParams(BaseModel):
    """HTTP PATCH - partially update a resource."""
    url: str = Field(..., description="Target URL.")
    headers: dict[str, str] | None = Field(None, description="Custom request headers.")
    json_body: dict | list | None = Field(None, description="JSON payload.")
    body: str | None = Field(None, description="Raw body.")
    timeout: float = Field(30.0, ge=1.0, le=300.0, description="Request timeout in seconds.")
    verify_tls: bool = Field(True, description="Verify TLS certificates.")


class DeleteParams(BaseModel):
    """HTTP DELETE - remove a resource."""
    url: str = Field(..., description="Target URL.")
    headers: dict[str, str] | None = Field(None, description="Custom request headers.")
    query_params: dict[str, str] | None = Field(None, description="URL query parameters.")
    timeout: float = Field(30.0, ge=1.0, le=300.0, description="Request timeout in seconds.")
    verify_tls: bool = Field(True, description="Verify TLS certificates.")


class HeadParams(BaseModel):
    """HTTP HEAD - retrieve headers without downloading the body."""
    url: str = Field(..., description="Target URL.")
    headers: dict[str, str] | None = Field(None, description="Custom request headers.")
    timeout: float = Field(15.0, ge=1.0, le=120.0, description="Request timeout in seconds.")
    verify_tls: bool = Field(True, description="Verify TLS certificates.")


class OptionsParams(BaseModel):
    """HTTP OPTIONS - discover allowed methods and CORS."""
    url: str = Field(..., description="Target URL.")
    headers: dict[str, str] | None = Field(None, description="Custom request headers.")
    timeout: float = Field(15.0, ge=1.0, le=120.0, description="Request timeout in seconds.")
    verify_tls: bool = Field(True, description="Verify TLS certificates.")


class JsonApiParams(BaseModel):
    """Call a JSON API endpoint with auto JSON handling.

    Examples:
        json_api(url="https://api.example.com/users", method="GET", auth_bearer="token123")
        json_api(url="https://api.example.com/users", method="POST", data={"name": "Alice"})
    """
    url: str = Field(..., description="API endpoint URL.")
    method: str = Field("GET", description="HTTP method.")
    data: dict | list | None = Field(None, description="Request payload (auto-serialized to JSON).")
    headers: dict[str, str] | None = Field(None, description="Custom request headers.")
    query_params: dict[str, str] | None = Field(None, description="URL query parameters.")
    auth_bearer: str | None = Field(None, description="Bearer token (auto-builds Authorization header).")
    timeout: float = Field(30.0, ge=1.0, le=300.0, description="Request timeout in seconds.")
    verify_tls: bool = Field(True, description="Verify TLS certificates.")


class SubmitFormParams(BaseModel):
    """Submit an HTML form (application/x-www-form-urlencoded)."""
    url: str = Field(..., description="Form action URL.")
    fields: dict[str, str] = Field(..., description="Form field key-value pairs.")
    method: str = Field("POST", description="HTTP method (POST or PUT).")
    headers: dict[str, str] | None = Field(None, description="Custom request headers.")
    timeout: float = Field(30.0, ge=1.0, le=300.0, description="Request timeout in seconds.")
    verify_tls: bool = Field(True, description="Verify TLS certificates.")


class UploadFileParams(BaseModel):
    """Upload a file via multipart/form-data.

    Examples:
        upload_file(url="https://api.example.com/upload", file_path="report.pdf")
        upload_file(url="https://api.example.com/upload", file_path="data.csv", field_name="document", extra_fields={"type": "csv"})
    """
    url: str = Field(..., description="Upload endpoint URL.")
    file_path: str = Field(..., description="Local file path to upload.")
    field_name: str = Field("file", description="Multipart form field name for the file.")
    extra_fields: dict[str, str] | None = Field(None, description="Additional form fields.")
    headers: dict[str, str] | None = Field(None, description="Custom request headers.")
    timeout: float = Field(120.0, ge=1.0, le=600.0, description="Upload timeout in seconds.")
    verify_tls: bool = Field(True, description="Verify TLS certificates.")
    max_upload_bytes: int = Field(100_000_000, ge=1, le=1_000_000_000, description="Max file size to upload (default 100 MB).")


class FetchPageParams(BaseModel):
    """Fetch a web page and extract readable text from HTML."""
    url: str = Field(..., description="Page URL to fetch.")
    headers: dict[str, str] | None = Field(None, description="Custom request headers.")
    timeout: float = Field(30.0, ge=1.0, le=120.0, description="Request timeout in seconds.")
    verify_tls: bool = Field(True, description="Verify TLS certificates.")
    max_response_bytes: int = Field(5_000_000, ge=1, le=50_000_000, description="Max HTML to download.")
    extract_links: bool = Field(True, description="Include list of links found on the page.")
    max_text_length: int = Field(50_000, ge=100, le=500_000, description="Truncate extracted text to this many characters.")


class DownloadParams(BaseModel):
    """Start a background file download."""
    url: str = Field(..., description="File URL to download.")
    destination: str = Field(..., description="Local file path to save to.")
    headers: dict[str, str] | None = Field(None, description="Custom request headers.")
    timeout: float = Field(3600.0, ge=1.0, le=86400.0, description="Total download timeout (up to 24h).")
    verify_tls: bool = Field(True, description="Verify TLS certificates.")
    overwrite: bool = Field(False, description="Overwrite if file already exists.")
    chunk_size: int = Field(65536, ge=4096, le=1_048_576, description="Download chunk size in bytes.")


class DownloadIdParams(BaseModel):
    """Identify a background download by ID."""
    download_id: str = Field(..., description="The download ID returned by download.")


class DownloadListParams(BaseModel):
    """List all background downloads."""
    pass
