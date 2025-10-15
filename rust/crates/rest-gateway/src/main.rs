use axum::{
    extract::{Json, Path, State},
    http::{HeaderMap, StatusCode},
    middleware,
    response::{IntoResponse, Response},
    routing::post,
    Router,
};
use jsonwebtoken::{decode, encode, DecodingKey, EncodingKey, Header, Validation};
use serde::{Deserialize, Serialize};
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};
use tonic::transport::Channel;
use tower_http::cors::{Any, CorsLayer};

// JWT Claims structure
#[derive(Debug, Clone, Serialize, Deserialize)]
struct Claims {
    sub: String,
    exp: usize,
}

// Application state
#[derive(Clone)]
struct AppState {
    jwt_secret: String,
    grpc_channel: Channel,
}

// Error type
#[derive(Debug)]
enum AppError {
    Unauthorized(String),
    BadRequest(String),
    Internal(String),
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let (status, message) = match self {
            AppError::Unauthorized(msg) => (StatusCode::UNAUTHORIZED, msg),
            AppError::BadRequest(msg) => (StatusCode::BAD_REQUEST, msg),
            AppError::Internal(msg) => (StatusCode::INTERNAL_SERVER_ERROR, msg),
        };
        (status, Json(serde_json::json!({"error": message}))).into_response()
    }
}

// JWT validation middleware
async fn jwt_auth_middleware(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    mut request: axum::extract::Request,
    next: middleware::Next,
) -> Result<Response, AppError> {
    let auth_header = headers
        .get("authorization")
        .and_then(|h| h.to_str().ok())
        .ok_or_else(|| AppError::Unauthorized("Authorization header required".to_string()))?;

    let token = auth_header
        .strip_prefix("Bearer ")
        .ok_or_else(|| AppError::Unauthorized("Invalid authorization format".to_string()))?;

    let validation = Validation::default();
    let token_data = decode::<Claims>(
        token,
        &DecodingKey::from_secret(state.jwt_secret.as_ref()),
        &validation,
    )
    .map_err(|e| AppError::Unauthorized(format!("Token validation error: {}", e)))?;

    // Check expiration
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs() as usize;

    if token_data.claims.exp < now {
        return Err(AppError::Unauthorized("Token expired".to_string()));
    }

    // Add claims to request extensions
    request.extensions_mut().insert(token_data.claims);

    Ok(next.run(request).await)
}

// Generic gRPC proxy handler
async fn proxy_grpc_request(
    State(state): State<Arc<AppState>>,
    Path((service, method)): Path<(String, String)>,
    Json(payload): Json<serde_json::Value>,
) -> Result<Json<serde_json::Value>, AppError> {
    // This is a simplified proxy - in production you'd need to:
    // 1. Parse the service/method to determine which gRPC client to use
    // 2. Convert JSON to protobuf
    // 3. Make the gRPC call
    // 4. Convert protobuf response back to JSON

    match service.as_str() {
        "show.ShowInterface" => handle_show_interface(&state, &method, payload).await,
        "job.JobInterface" => handle_job_interface(&state, &method, payload).await,
        "frame.FrameInterface" => handle_frame_interface(&state, &method, payload).await,
        "layer.LayerInterface" => handle_layer_interface(&state, &method, payload).await,
        "group.GroupInterface" => handle_group_interface(&state, &method, payload).await,
        "host.HostInterface" => handle_host_interface(&state, &method, payload).await,
        "owner.OwnerInterface" => handle_owner_interface(&state, &method, payload).await,
        "proc.ProcInterface" => handle_proc_interface(&state, &method, payload).await,
        "deed.DeedInterface" => handle_deed_interface(&state, &method, payload).await,
        _ => Err(AppError::BadRequest(format!("Unknown service: {}", service))),
    }
}

// Show interface handlers
async fn handle_show_interface(
    state: &AppState,
    method: &str,
    payload: serde_json::Value,
) -> Result<Json<serde_json::Value>, AppError> {
    // You'll need to generate gRPC clients from your proto files
    // For now, this is a placeholder showing the structure

    match method {
        "GetShows" => {
            // Example: call gRPC client
            // let mut client = show_interface_client::ShowInterfaceClient::new(state.grpc_channel.clone());
            // let request = tonic::Request::new(GetShowsRequest {});
            // let response = client.get_shows(request).await
            //     .map_err(|e| AppError::Internal(format!("gRPC error: {}", e)))?;
            // Ok(Json(serde_json::to_value(response.into_inner()).unwrap()))

            // Placeholder response
            Ok(Json(serde_json::json!({
                "shows": {
                    "shows": []
                }
            })))
        }
        "FindShow" => {
            let name = payload.get("name")
                .and_then(|v| v.as_str())
                .ok_or_else(|| AppError::BadRequest("Missing 'name' field".to_string()))?;

            // Make gRPC call with name parameter
            Ok(Json(serde_json::json!({
                "show": {
                    "name": name,
                    "id": "placeholder"
                }
            })))
        }
        _ => Err(AppError::BadRequest(format!("Unknown method: {}", method))),
    }
}

// Job interface handlers
async fn handle_job_interface(
    state: &AppState,
    method: &str,
    payload: serde_json::Value,
) -> Result<Json<serde_json::Value>, AppError> {
    match method {
        "GetJobs" => {
            Ok(Json(serde_json::json!({
                "jobs": {
                    "jobs": []
                }
            })))
        }
        "FindJob" => {
            Ok(Json(serde_json::json!({
                "job": {}
            })))
        }
        "Kill" | "Pause" | "Resume" => {
            Ok(Json(serde_json::json!({})))
        }
        _ => Err(AppError::BadRequest(format!("Unknown method: {}", method))),
    }
}

// Placeholder handlers for other interfaces
async fn handle_frame_interface(
    state: &AppState,
    method: &str,
    payload: serde_json::Value,
) -> Result<Json<serde_json::Value>, AppError> {
    Ok(Json(serde_json::json!({})))
}

async fn handle_layer_interface(
    state: &AppState,
    method: &str,
    payload: serde_json::Value,
) -> Result<Json<serde_json::Value>, AppError> {
    Ok(Json(serde_json::json!({})))
}

async fn handle_group_interface(
    state: &AppState,
    method: &str,
    payload: serde_json::Value,
) -> Result<Json<serde_json::Value>, AppError> {
    Ok(Json(serde_json::json!({})))
}

async fn handle_host_interface(
    state: &AppState,
    method: &str,
    payload: serde_json::Value,
) -> Result<Json<serde_json::Value>, AppError> {
    Ok(Json(serde_json::json!({})))
}

async fn handle_owner_interface(
    state: &AppState,
    method: &str,
    payload: serde_json::Value,
) -> Result<Json<serde_json::Value>, AppError> {
    Ok(Json(serde_json::json!({})))
}

async fn handle_proc_interface(
    state: &AppState,
    method: &str,
    payload: serde_json::Value,
) -> Result<Json<serde_json::Value>, AppError> {
    Ok(Json(serde_json::json!({})))
}

async fn handle_deed_interface(
    state: &AppState,
    method: &str,
    payload: serde_json::Value,
) -> Result<Json<serde_json::Value>, AppError> {
    Ok(Json(serde_json::json!({})))
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Load configuration from environment
    let cuebot_endpoint = std::env::var("CUEBOT_ENDPOINT")
        .unwrap_or_else(|_| "http://localhost:8443".to_string());
    let rest_port = std::env::var("REST_PORT")
        .unwrap_or_else(|_| "8448".to_string())
        .parse::<u16>()?;
    let jwt_secret = std::env::var("JWT_SECRET")
        .unwrap_or_else(|_| "default-secret-key".to_string());

    println!("Connecting to Cuebot at: {}", cuebot_endpoint);

    // Create gRPC channel
    let grpc_channel = Channel::from_shared(cuebot_endpoint)?
        .connect()
        .await?;

    // Create application state
    let state = Arc::new(AppState {
        jwt_secret,
        grpc_channel,
    });

    // Build CORS layer
    let cors = CorsLayer::new()
        .allow_origin(Any)
        .allow_methods(Any)
        .allow_headers(Any);

    // Build router
    let app = Router::new()
        .route("/:service/:method", post(proxy_grpc_request))
        .layer(middleware::from_fn_with_state(state.clone(), jwt_auth_middleware))
        .layer(cors)
        .with_state(state);

    // Create socket address
    let addr = SocketAddr::from(([0, 0, 0, 0], rest_port));
    println!("REST Gateway listening on: {}", addr);

    // Run server
    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, app).await?;

    Ok(())
}