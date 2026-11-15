// Minimal std-only JSON reader/writer helpers (baseline model persistence + HTTP output)

#[derive(Debug, Clone)]
pub enum Json {
    Null,
    Bool(bool),
    Num(f64),
    Str(String),
    Arr(Vec<Json>),
    Obj(Vec<(String, Json)>),
}

impl Json {
    pub fn get(&self, k: &str) -> Option<&Json> {
        match self {
            Json::Obj(v) => v.iter().find(|(kk, _)| kk == k).map(|(_, val)| val),
            _ => None,
        }
    }
    pub fn as_f64(&self) -> Option<f64> {
        match self {
            Json::Num(n) => Some(*n),
            _ => None,
        }
    }
    pub fn as_u64(&self) -> Option<u64> {
        self.as_f64().map(|n| n as u64)
    }
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(s) => Some(s),
            _ => None,
        }
    }
    pub fn as_arr(&self) -> Option<&Vec<Json>> {
        match self {
            Json::Arr(v) => Some(v),
            _ => None,
        }
    }
    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Json::Bool(b) => Some(*b),
            _ => None,
        }
    }
    #[allow(dead_code)]
    pub fn as_obj(&self) -> Option<&Vec<(String, Json)>> {
        match self {
            Json::Obj(v) => Some(v),
            _ => None,
        }
    }
}

pub fn escape(s: &str) -> String {
    let mut out = String::new();
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

pub fn write(v: &Json) -> String {
    match v {
        Json::Null => "null".into(),
        Json::Bool(b) => b.to_string(),
        Json::Num(n) => {
            if n.is_finite() {
                if n.fract() == 0.0 && n.abs() < 1e15 {
                    format!("{}", *n as i64)
                } else {
                    format!("{}", n)
                }
            } else {
                "null".into()
            }
        }
        Json::Str(s) => format!("\"{}\"", escape(s)),
        Json::Arr(items) => {
            let body: Vec<String> = items.iter().map(write).collect();
            format!("[{}]", body.join(","))
        }
        Json::Obj(entries) => {
            let body: Vec<String> = entries
                .iter()
                .map(|(k, val)| format!("\"{}\":{}", escape(k), write(val)))
                .collect();
            format!("{{{}}}", body.join(","))
        }
    }
}

pub fn parse(s: &str) -> Option<Json> {
    let b = s.as_bytes();
    let mut i = 0usize;
    let v = parse_val(b, &mut i)?;
    ws(b, &mut i);
    if i == b.len() {
        Some(v)
    } else {
        None
    }
}

fn ws(b: &[u8], i: &mut usize) {
    while *i < b.len() && b[*i].is_ascii_whitespace() {
        *i += 1;
    }
}

fn parse_val(b: &[u8], i: &mut usize) -> Option<Json> {
    ws(b, i);
    match *b.get(*i)? {
        b'"' => parse_str(b, i).map(Json::Str),
        b'{' => {
            *i += 1;
            let mut entries = Vec::new();
            ws(b, i);
            if *b.get(*i)? == b'}' {
                *i += 1;
                return Some(Json::Obj(entries));
            }
            loop {
                ws(b, i);
                let key = parse_str(b, i)?;
                ws(b, i);
                if *b.get(*i)? != b':' {
                    return None;
                }
                *i += 1;
                let val = parse_val(b, i)?;
                entries.push((key, val));
                ws(b, i);
                match *b.get(*i)? {
                    b',' => *i += 1,
                    b'}' => {
                        *i += 1;
                        return Some(Json::Obj(entries));
                    }
                    _ => return None,
                }
            }
        }
        b'[' => {
            *i += 1;
            let mut items = Vec::new();
            ws(b, i);
            if *b.get(*i)? == b']' {
                *i += 1;
                return Some(Json::Arr(items));
            }
            loop {
                items.push(parse_val(b, i)?);
                ws(b, i);
                match *b.get(*i)? {
                    b',' => *i += 1,
                    b']' => {
                        *i += 1;
                        return Some(Json::Arr(items));
                    }
                    _ => return None,
                }
            }
        }
        b't' => lit(b, i, "true", Json::Bool(true)),
        b'f' => lit(b, i, "false", Json::Bool(false)),
        b'n' => lit(b, i, "null", Json::Null),
        _ => {
            let start = *i;
            while *i < b.len() && b"0123456789+-.eE".contains(&b[*i]) {
                *i += 1;
            }
            if *i == start {
                return None;
            }
            std::str::from_utf8(&b[start..*i]).ok()?.parse::<f64>().ok().map(Json::Num)
        }
    }
}

fn lit(b: &[u8], i: &mut usize, word: &str, v: Json) -> Option<Json> {
    if b[*i..].starts_with(word.as_bytes()) {
        *i += word.len();
        Some(v)
    } else {
        None
    }
}

fn parse_str(b: &[u8], i: &mut usize) -> Option<String> {
    if *b.get(*i)? != b'"' {
        return None;
    }
    *i += 1;
    let mut out = String::new();
    loop {
        let c = *b.get(*i)?;
        *i += 1;
        match c {
            b'"' => return Some(out),
            b'\\' => {
                let e = *b.get(*i)?;
                *i += 1;
                match e {
                    b'"' => out.push('"'),
                    b'\\' => out.push('\\'),
                    b'/' => out.push('/'),
                    b'b' => out.push('\u{0008}'),
                    b'f' => out.push('\u{000C}'),
                    b'n' => out.push('\n'),
                    b'r' => out.push('\r'),
                    b't' => out.push('\t'),
                    b'u' => {
                        let cp = hex4(b, i)?;
                        if (0xD800..=0xDBFF).contains(&cp) {
                            if b.get(*i) == Some(&b'\\') && b.get(*i + 1) == Some(&b'u') {
                                *i += 2;
                                let lo = hex4(b, i)?;
                                let combined = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                                out.push(char::from_u32(combined)?);
                            } else {
                                return None;
                            }
                        } else {
                            out.push(char::from_u32(cp)?);
                        }
                    }
                    _ => return None,
                }
            }
            _ => {
                // Collect one UTF-8 sequence (input is valid UTF-8 &str).
                let start = *i - 1;
                let len = utf8_len(c);
                *i = start + len;
                out.push_str(std::str::from_utf8(&b[start..*i]).ok()?);
            }
        }
    }
}

fn utf8_len(first: u8) -> usize {
    if first < 0x80 {
        1
    } else if first >> 5 == 0b110 {
        2
    } else if first >> 4 == 0b1110 {
        3
    } else {
        4
    }
}

fn hex4(b: &[u8], i: &mut usize) -> Option<u32> {
    if *i + 4 > b.len() {
        return None;
    }
    let s = std::str::from_utf8(&b[*i..*i + 4]).ok()?;
    let v = u32::from_str_radix(s, 16).ok()?;
    *i += 4;
    Some(v)
}