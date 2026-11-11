// Port of src/safehalo/auth_manager.py: PBKDF2-HMAC-SHA256 users.json,
// in-memory sessions and RBAC. Keeps the on-disk format Python wrote.

use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use crate::json;
use crate::timefmt;

const ITERATIONS: u32 = 100_000;
const SESSION_SECONDS: f64 = 7.0 * 24.0 * 3600.0;

// ---------------------------------------------------------------- SHA-256

const K: [u32; 64] = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];

pub fn sha256(data: &[u8]) -> [u8; 32] {
    let mut h: [u32; 8] = [
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
    ];
    let mut msg = data.to_vec();
    let bit_len = (data.len() as u64).wrapping_mul(8);
    msg.push(0x80);
    while msg.len() % 64 != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&bit_len.to_be_bytes());

    let mut w = [0u32; 64];
    for chunk in msg.chunks_exact(64) {
        for i in 0..16 {
            w[i] = u32::from_be_bytes([chunk[i * 4], chunk[i * 4 + 1], chunk[i * 4 + 2], chunk[i * 4 + 3]]);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16].wrapping_add(s0).wrapping_add(w[i - 7]).wrapping_add(s1);
        }
        let (mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut hh) =
            (h[0], h[1], h[2], h[3], h[4], h[5], h[6], h[7]);
        for i in 0..64 {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ ((!e) & g);
            let t1 = hh.wrapping_add(s1).wrapping_add(ch).wrapping_add(K[i]).wrapping_add(w[i]);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let t2 = s0.wrapping_add(maj);
            hh = g;
            g = f;
            f = e;
            e = d.wrapping_add(t1);
            d = c;
            c = b;
            b = a;
            a = t1.wrapping_add(t2);
        }
        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
        h[5] = h[5].wrapping_add(f);
        h[6] = h[6].wrapping_add(g);
        h[7] = h[7].wrapping_add(hh);
    }
    let mut out = [0u8; 32];
    for (i, v) in h.iter().enumerate() {
        out[i * 4..i * 4 + 4].copy_from_slice(&v.to_be_bytes());
    }
    out
}

fn hmac_sha256(key: &[u8], msg: &[u8]) -> [u8; 32] {
    let mut k = [0u8; 64];
    if key.len() > 64 {
        k[..32].copy_from_slice(&sha256(key));
    } else {
        k[..key.len()].copy_from_slice(key);
    }
    let mut inner = Vec::with_capacity(64 + msg.len());
    for b in &k {
        inner.push(b ^ 0x36);
    }
    inner.extend_from_slice(msg);
    let ih = sha256(&inner);
    let mut outer = Vec::with_capacity(96);
    for b in &k {
        outer.push(b ^ 0x5c);
    }
    outer.extend_from_slice(&ih);
    sha256(&outer)
}

pub fn pbkdf2_sha256(password: &str, salt: &[u8], iterations: u32) -> [u8; 32] {
    // dkLen = 32 -> exactly one block.
    let mut m = Vec::with_capacity(salt.len() + 4);
    m.extend_from_slice(salt);
    m.extend_from_slice(&1u32.to_be_bytes());
    let mut u = hmac_sha256(password.as_bytes(), &m);
    let mut t = u;
    for _ in 1..iterations {
        u = hmac_sha256(password.as_bytes(), &u);
        for i in 0..32 {
            t[i] ^= u[i];
        }
    }
    t
}

fn hex_encode(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push_str(&format!("{:02x}", b));
    }
    s
}

fn hex_decode(s: &str) -> Option<Vec<u8>> {
    if s.len() % 2 != 0 {
        return None;
    }
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(s.len() / 2);
    for i in (0..b.len()).step_by(2) {
        let hi = (b[i] as char).to_digit(16)? as u8;
        let lo = (b[i + 1] as char).to_digit(16)? as u8;
        out.push((hi << 4) | lo);
    }
    Some(out)
}

fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut d = 0u8;
    for i in 0..a.len() {
        d |= a[i] ^ b[i];
    }
    d == 0
}

pub fn random_bytes(n: usize) -> Vec<u8> {
    use std::io::Read;
    let mut buf = vec![0u8; n];
    if let Ok(mut f) = fs::File::open("/dev/urandom") {
        if f.read_exact(&mut buf).is_ok() {
            return buf;
        }
    }
    // Extremely unlikely fallback; still avoid a fully predictable token.
    let seed = format!("{}-{}-{:?}", timefmt::now_epoch(), std::process::id(), std::thread::current().id());
    let mut h = sha256(seed.as_bytes());
    for i in 0..n {
        if i % 32 == 0 && i > 0 {
            h = sha256(&h);
        }
        buf[i] = h[i % 32];
    }
    buf
}

fn password_policy_error(password: &str) -> Option<String> {
    if password.len() < 8 {
        return Some("密码长度至少需 8 个字符".into());
    }
    if password == "admin123" {
        return Some("不能使用默认密码 admin123，请设置一个新密码".into());
    }
    None
}

pub fn hash_password(password: &str, salt_hex: Option<&str>) -> Option<(String, String)> {
    let salt = match salt_hex {
        Some(s) => hex_decode(s)?,
        None => random_bytes(16),
    };
    let dk = pbkdf2_sha256(password, &salt, ITERATIONS);
    Some((hex_encode(&dk), hex_encode(&salt)))
}

pub fn verify_password(password: &str, stored_hash: &str, salt_hex: &str) -> bool {
    match hash_password(password, Some(salt_hex)) {
        Some((h, _)) => ct_eq(h.as_bytes(), stored_hash.as_bytes()),
        None => false,
    }
}

// ---------------------------------------------------------------- users

#[derive(Clone)]
pub struct User {
    pub username: String,
    pub display_name: String,
    pub role: String,
    pub password_hash: String,
    pub salt: String,
    pub created_at: String,
    pub last_login: Option<String>,
    pub must_change_password: bool,
}

#[derive(Clone)]
pub struct Session {
    pub username: String,
    pub display_name: String,
    pub role: String,
    pub must_change_password: bool,
    pub created_at: f64,
    pub expires_at: f64,
}

pub struct AuthManager {
    file: PathBuf,
    state: Mutex<AuthState>,
}

struct AuthState {
    users: Vec<User>,
    sessions: HashMap<String, Session>,
}

fn get_str<'a>(v: &'a json::Json, k: &str) -> String {
    v.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string()
}

fn load_users(file: &Path) -> Vec<User> {
    let text = match fs::read_to_string(file) {
        Ok(t) => t,
        Err(_) => return Vec::new(),
    };
    let root = match json::parse(&text) {
        Some(j) => j,
        None => return Vec::new(),
    };
    let mut users = Vec::new();
    if let json::Json::Obj(entries) = root {
        for (_, v) in entries {
            let username = get_str(&v, "username");
            if username.is_empty() {
                continue;
            }
            users.push(User {
                display_name: {
                    let d = get_str(&v, "display_name");
                    if d.is_empty() { username.clone() } else { d }
                },
                role: {
                    let r = get_str(&v, "role");
                    if r.is_empty() { "family".into() } else { r }
                },
                password_hash: get_str(&v, "password_hash"),
                salt: get_str(&v, "salt"),
                created_at: get_str(&v, "created_at"),
                last_login: v.get("last_login").and_then(|x| x.as_str()).map(|s| s.to_string()),
                must_change_password: v
                    .get("must_change_password")
                    .and_then(|x| x.as_bool())
                    .unwrap_or(false),
                username,
            });
        }
    }
    users
}

fn user_json(u: &User, pretty: bool) -> String {
    let indent = if pretty { "    " } else { "" };
    let nl = if pretty { "\n" } else { "" };
    let last_login = match &u.last_login {
        Some(s) => format!("\"{}\"", json::escape(s)),
        None => "null".into(),
    };
    format!(
        "{{{nl}{indent}\"username\": \"{}\",{nl}{indent}\"display_name\": \"{}\",{nl}{indent}\"role\": \"{}\",{nl}{indent}\"password_hash\": \"{}\",{nl}{indent}\"salt\": \"{}\",{nl}{indent}\"created_at\": \"{}\",{nl}{indent}\"last_login\": {},{nl}{indent}\"must_change_password\": {}{nl}}}",
        json::escape(&u.username),
        json::escape(&u.display_name),
        json::escape(&u.role),
        u.password_hash,
        u.salt,
        json::escape(&u.created_at),
        last_login,
        u.must_change_password,
        nl = nl,
        indent = indent
    )
}

fn save_users(file: &Path, users: &[User]) -> bool {
    if let Some(dir) = file.parent() {
        let _ = fs::create_dir_all(dir);
    }
    let mut out = String::from("{\n");
    for (i, u) in users.iter().enumerate() {
        out.push_str(&format!(
            "  \"{}\": {}{}",
            json::escape(&u.username),
            user_json(u, true),
            if i + 1 == users.len() { "\n" } else { ",\n" }
        ));
    }
    out.push_str("}\n");
    fs::write(file, out).is_ok()
}

impl AuthManager {
    pub fn new(data_dir: &str) -> AuthManager {
        let file = Path::new(data_dir).join("users.json");
        let mut users = load_users(&file);
        if users.is_empty() {
            let (hash, salt) = hash_password("admin123", None).unwrap();
            users.push(User {
                username: "admin".into(),
                display_name: "系统管理员".into(),
                role: "admin".into(),
                password_hash: hash,
                salt,
                created_at: timefmt::now().date_time[..19].to_string(),
                last_login: None,
                must_change_password: true,
            });
            save_users(&file, &users);
            eprintln!("[auth] created default account admin/admin123 (change it after first login)");
        }
        AuthManager {
            file,
            state: Mutex::new(AuthState {
                users,
                sessions: HashMap::new(),
            }),
        }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, AuthState> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    pub fn login(&self, username: &str, password: &str, client_ip: &str) -> Result<(String, Session), String> {
        let mut st = self.lock();
        let idx = match st.users.iter().position(|u| u.username == username) {
            Some(i) => i,
            None => {
                eprintln!("[auth] login failed: unknown user [{}] from {}", username, client_ip);
                return Err("用户名或密码错误".into());
            }
        };
        if !verify_password(password, &st.users[idx].password_hash, &st.users[idx].salt) {
            eprintln!("[auth] login failed: bad password [{}] from {}", username, client_ip);
            return Err("用户名或密码错误".into());
        }
        let token = hex_encode(&random_bytes(32));
        let now = timefmt::now_epoch();
        let sess = Session {
            username: st.users[idx].username.clone(),
            display_name: st.users[idx].display_name.clone(),
            role: st.users[idx].role.clone(),
            must_change_password: st.users[idx].must_change_password,
            created_at: now,
            expires_at: now + SESSION_SECONDS,
        };
        st.sessions.insert(token.clone(), sess.clone());
        st.users[idx].last_login = Some(timefmt::now().date_time[..19].to_string());
        let users = st.users.clone();
        drop(st);
        save_users(&self.file, &users);
        eprintln!("[auth] login ok: [{}] role={} from {}", username, sess.role, client_ip);
        Ok((token, sess))
    }

    pub fn logout(&self, token: &str) {
        let mut st = self.lock();
        st.sessions.remove(token);
    }

    pub fn validate(&self, token: &str) -> Option<Session> {
        let mut st = self.lock();
        let now = timefmt::now_epoch();
        match st.sessions.get(token) {
            Some(s) if now <= s.expires_at => Some(s.clone()),
            Some(_) => {
                st.sessions.remove(token);
                None
            }
            None => None,
        }
    }

    pub fn list_users(&self) -> Vec<UserView> {
        let st = self.lock();
        st.users
            .iter()
            .map(|u| UserView {
                username: u.username.clone(),
                display_name: u.display_name.clone(),
                role: u.role.clone(),
                created_at: u.created_at.clone(),
                last_login: u.last_login.clone().unwrap_or_else(|| "从未登录".into()),
                must_change_password: u.must_change_password,
            })
            .collect()
    }

    pub fn create_user(&self, username: &str, password: &str, role: &str, display_name: &str) -> (bool, String) {
        let username = username.trim();
        if username.len() < 3 {
            return (false, "用户名长度至少需 3 个字符".into());
        }
        if let Some(e) = password_policy_error(password) {
            return (false, e);
        }
        if role != "admin" && role != "family" {
            return (false, "非法角色，仅限 admin 或 family".into());
        }
        let mut st = self.lock();
        if st.users.iter().any(|u| u.username == username) {
            return (false, format!("用户 [{}] 已存在", username));
        }
        let (hash, salt) = hash_password(password, None).unwrap();
        st.users.push(User {
            username: username.to_string(),
            display_name: if display_name.trim().is_empty() {
                username.to_string()
            } else {
                display_name.trim().to_string()
            },
            role: role.to_string(),
            password_hash: hash,
            salt,
            created_at: timefmt::now().date_time[..19].to_string(),
            last_login: None,
            must_change_password: false,
        });
        let users = st.users.clone();
        drop(st);
        save_users(&self.file, &users);
        (true, "创建成功".into())
    }

    pub fn update_password(&self, username: &str, old: &str, new: &str) -> (bool, String) {
        if let Some(e) = password_policy_error(new) {
            return (false, e);
        }
        if new == old {
            return (false, "新密码不能与原密码相同".into());
        }
        let mut st = self.lock();
        let idx = match st.users.iter().position(|u| u.username == username) {
            Some(i) => i,
            None => return (false, "用户不存在".into()),
        };
        if !verify_password(old, &st.users[idx].password_hash, &st.users[idx].salt) {
            return (false, "原密码错误".into());
        }
        let (hash, salt) = hash_password(new, None).unwrap();
        st.users[idx].password_hash = hash;
        st.users[idx].salt = salt;
        st.users[idx].must_change_password = false;
        st.sessions.retain(|_, s| s.username != username);
        let users = st.users.clone();
        drop(st);
        save_users(&self.file, &users);
        (true, "密码修改成功，请重新登录".into())
    }

    pub fn delete_user(&self, username: &str, operator: &str) -> (bool, String) {
        if username == operator {
            return (false, "无法删除当前登录的账号".into());
        }
        let mut st = self.lock();
        let idx = match st.users.iter().position(|u| u.username == username) {
            Some(i) => i,
            None => return (false, "用户不存在".into()),
        };
        let admin_count = st.users.iter().filter(|u| u.role == "admin").count();
        if st.users[idx].role == "admin" && admin_count <= 1 {
            return (false, "系统必须保留至少一个管理员账号".into());
        }
        st.users.remove(idx);
        st.sessions.retain(|_, s| s.username != username);
        let users = st.users.clone();
        drop(st);
        save_users(&self.file, &users);
        (true, "用户已成功删除".into())
    }

    pub fn default_admin_path(&self) -> String {
        self.file.display().to_string()
    }
}

pub struct UserView {
    pub username: String,
    pub display_name: String,
    pub role: String,
    pub created_at: String,
    pub last_login: String,
    pub must_change_password: bool,
}

impl UserView {
    pub fn to_json(&self) -> String {
        format!(
            "{{\"username\":\"{}\",\"display_name\":\"{}\",\"role\":\"{}\",\"created_at\":\"{}\",\"last_login\":\"{}\",\"must_change_password\":{}}}",
            json::escape(&self.username),
            json::escape(&self.display_name),
            json::escape(&self.role),
            json::escape(&self.created_at),
            json::escape(&self.last_login),
            self.must_change_password
        )
    }
}

impl Session {
    pub fn to_json(&self) -> String {
        format!(
            "{{\"username\":\"{}\",\"display_name\":\"{}\",\"role\":\"{}\",\"must_change_password\":{},\"created_at\":{},\"expires_at\":{}}}",
            json::escape(&self.username),
            json::escape(&self.display_name),
            json::escape(&self.role),
            self.must_change_password,
            self.created_at,
            self.expires_at
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pbkdf2_matches_python_hashlib() {
        let salt = hex_decode("00112233445566778899aabbccddeeff").unwrap();
        let dk = pbkdf2_sha256("admin123", &salt, 100_000);
        assert_eq!(
            hex_encode(&dk),
            "be8187761f01504afba863a09eda647c096c7cac8cc7f0b3351fc32dba1e014e"
        );
        let dk = pbkdf2_sha256("password", &salt, 100_000);
        assert_eq!(
            hex_encode(&dk),
            "fa97960c9c5fe8a3fb30d57bc7a6786f6b3504dfe3f9caaf886b28c7f16d072b"
        );
    }

    #[test]
    fn hash_verify_and_user_flow() {
        let dir = std::env::temp_dir().join(format!("safehalo_auth_{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let auth = AuthManager::new(dir.to_str().unwrap());
        let (hash, salt) = {
            let st = auth.lock();
            (st.users[0].password_hash.clone(), st.users[0].salt.clone())
        };
        assert!(verify_password("admin123", &hash, &salt));
        let (ok, msg) = auth.create_user("family1", "secret123", "family", "家属");
        assert!(ok, "{}", msg);
        let (token, sess) = auth.login("family1", "secret123", "test").unwrap();
        assert_eq!(sess.role, "family");
        assert!(auth.validate(&token).is_some());
        let (ok, _) = auth.update_password("family1", "secret123", "newpass1");
        assert!(ok);
        assert!(auth.login("family1", "secret123", "test").is_err());
        assert!(auth.login("family1", "newpass1", "test").is_ok());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn reads_python_users_json() {
        let dir = std::env::temp_dir().join(format!("safehalo_auth_py_{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let text = r#"{
  "admin": {
    "username": "admin",
    "display_name": "系统管理员",
    "role": "admin",
    "password_hash": "be8187761f01504afba863a09eda647c096c7cac8cc7f0b3351fc32dba1e014e",
    "salt": "00112233445566778899aabbccddeeff",
    "created_at": "2026-11-09 17:01:00",
    "last_login": null,
    "must_change_password": true
  }
}"#;
        fs::write(dir.join("users.json"), text).unwrap();
        let auth = AuthManager::new(dir.to_str().unwrap());
        let (token, sess) = auth.login("admin", "admin123", "test").unwrap();
        assert_eq!(sess.role, "admin");
        assert!(auth.validate(&token).is_some());
        let _ = fs::remove_dir_all(&dir);
    }
}
