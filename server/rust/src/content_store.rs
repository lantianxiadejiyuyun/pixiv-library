//! Bounded, disk-backed staging. Immutable blobs become visible before a guarded
//! database commit; interruption can leave an orphan, never a half-written asset.
use crate::{
    AppState,
    db::{Tx, now},
    error::{ApiError, Result},
    tasks::{Context, TaskResult},
};
use image::{ImageFormat, ImageReader};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::HashSet,
    io::{Cursor, Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
};
use tokio::io::AsyncWriteExt;
use unicode_normalization::UnicodeNormalization;

pub const MAX_FILE: u64 = 128 * 1024 * 1024;
pub const MAX_WORK: u64 = 512 * 1024 * 1024;
pub const MAX_FILES: usize = 3000;
#[derive(Clone)]
pub struct Entry {
    pub name: String,
    pub offset: u64,
    pub size: u64,
    pub inline_key: Option<String>,
}
pub struct Staging {
    file: tokio::fs::File,
    pub entries: Vec<Entry>,
    total: u64,
}
impl Staging {
    pub async fn stream<S, E>(
        &mut self,
        name: &str,
        stream: S,
        inline_key: Option<String>,
    ) -> TaskResult<()>
    where
        S: futures_util::Stream<Item = std::result::Result<axum::body::Bytes, E>> + Send,
    {
        use futures_util::StreamExt;
        let mut stream = Box::pin(stream);
        let mut size = 0u64;
        while let Some(chunk) = stream.next().await {
            let chunk = chunk.map_err(|_| ApiError::bad("文件传输中断"))?;
            size += chunk.len() as u64;
            if size > MAX_FILE || self.total + size > MAX_WORK {
                return Err(ApiError::bad("文件传输超过大小限制").into());
            }
            self.file.write_all(&chunk).await?;
        }
        if size == 0 {
            return Err(ApiError::bad("文件为空").into());
        }
        self.reserve(name, size, inline_key)?;
        Ok(())
    }
    pub async fn expand(self, s: &AppState) -> TaskResult<Self> {
        let (mut input, mut entries) = self.into_std().await?;
        let cpu = s
            .cpu_limit
            .clone()
            .acquire_owned()
            .await
            .map_err(|_| ApiError::bad("解压处理器不可用"))?;
        entries.sort_by(|a, b| natural(&a.name, &b.name));
        tokio::task::spawn_blocking(move || -> TaskResult<Self> {
            let _cpu = cpu;
            let mut output = tempfile::tempfile()?;
            let mut files = vec![];
            let mut total = 0u64;
            for entry in entries {
                let ext = extension(&entry.name);
                if ["zip", "cbz"].contains(&ext.as_str()) {
                    let bytes = read_entry(&mut input, &entry)?;
                    let mut archive = zip::ZipArchive::new(Cursor::new(bytes))
                        .map_err(|_| ApiError::bad("无法读取 ZIP/CBZ 压缩包"))?;
                    if archive.len() > MAX_FILES {
                        return Err(ApiError::bad("压缩包文件数量过多").into());
                    }
                    let mut order = vec![];
                    for i in 0..archive.len() {
                        let file = archive
                            .by_index(i)
                            .map_err(|_| ApiError::bad("压缩包损坏或已加密"))?;
                        let raw = file.name().replace('\\', "/");
                        if file.enclosed_name().is_none()
                            || raw.starts_with('/')
                            || raw.split('/').any(|p| p == "..")
                            || raw.contains([':', '\0'])
                            || file.unix_mode().is_some_and(|m| m & 0o170000 == 0o120000)
                        {
                            return Err(ApiError::bad("压缩包包含不安全路径或符号链接").into());
                        }
                        if !file.is_dir() && supported(&raw) {
                            order.push((i, raw));
                        }
                    }
                    order.sort_by(|a, b| natural(&a.1, &b.1));
                    for (i, name) in order {
                        let mut file = archive
                            .by_index(i)
                            .map_err(|_| ApiError::bad("压缩包损坏或已加密"))?;
                        let size = file.size();
                        if size > MAX_FILE
                            || size > file.compressed_size().max(1).saturating_mul(1000)
                            || total + size > MAX_WORK
                            || files.len() >= MAX_FILES
                        {
                            return Err(
                                ApiError::bad("压缩包解压大小、数量或压缩比超过限制").into()
                            );
                        }
                        let written =
                            std::io::copy(&mut (&mut file).take(MAX_FILE + 1), &mut output)
                                .map_err(|_| ApiError::bad("压缩包解压失败"))?;
                        if written != size {
                            return Err(ApiError::bad("压缩包文件大小不匹配").into());
                        }
                        files.push(Entry {
                            name: clean_name(&name)?,
                            offset: total,
                            size,
                            inline_key: None,
                        });
                        total += size;
                    }
                } else {
                    if !supported(&entry.name) {
                        return Err(ApiError::bad("不支持的文件格式").into());
                    }
                    if total + entry.size > MAX_WORK || files.len() >= MAX_FILES {
                        return Err(ApiError::bad("导入文件总量超过限制").into());
                    }
                    input.seek(SeekFrom::Start(entry.offset))?;
                    let size = std::io::copy(&mut (&mut input).take(entry.size), &mut output)?;
                    if size != entry.size {
                        return Err(ApiError::bad("暂存文件不完整").into());
                    }
                    files.push(Entry {
                        name: entry.name,
                        offset: total,
                        size,
                        inline_key: entry.inline_key,
                    });
                    total += size;
                }
            }
            if files.is_empty() {
                return Err(ApiError::bad("未找到支持的图片、视频或 UTF-8 文本文件").into());
            }
            output.flush()?;
            Ok(Self {
                file: tokio::fs::File::from_std(output),
                entries: files,
                total,
            })
        })
        .await
        .map_err(|_| ApiError::bad("压缩包处理失败"))?
    }
    pub fn new() -> Result<Self> {
        Ok(Self {
            file: tokio::fs::File::from_std(tempfile::tempfile()?),
            entries: vec![],
            total: 0,
        })
    }
    pub async fn bytes(
        &mut self,
        name: &str,
        data: &[u8],
        inline_key: Option<String>,
    ) -> TaskResult<()> {
        if data.is_empty() || data.len() as u64 > MAX_FILE {
            return Err(ApiError::bad("文件为空或超过单文件 128 MiB 限制").into());
        }
        self.reserve(name, data.len() as u64, inline_key)?;
        self.file.write_all(data).await?;
        Ok(())
    }
    fn reserve(&mut self, name: &str, size: u64, inline_key: Option<String>) -> Result<()> {
        if self.entries.len() >= MAX_FILES || self.total + size > MAX_WORK {
            return Err(ApiError::bad("作品超过 512 MiB 或 3000 个文件限制"));
        }
        let name = clean_name(name)?;
        self.entries.push(Entry {
            name,
            offset: self.total,
            size,
            inline_key,
        });
        self.total += size;
        Ok(())
    }
    pub async fn response(
        &mut self,
        name: &str,
        mut response: reqwest::Response,
        inline_key: Option<String>,
        ctx: Option<&Context>,
    ) -> TaskResult<()> {
        if response
            .content_length()
            .is_some_and(|n| n > MAX_FILE || n + self.total > MAX_WORK)
        {
            return Err(ApiError::bad("远程文件超过下载大小限制").into());
        }
        let start = self.total;
        let mut size = 0u64;
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|_| ApiError::bad("下载中断，请在重试队列重试"))?
        {
            size += chunk.len() as u64;
            if size > MAX_FILE || start + size > MAX_WORK {
                return Err(ApiError::bad("作品超过下载大小限制").into());
            }
            if ctx.is_some_and(|c| c.stop.is_cancelled()) {
                return Err(crate::tasks::TaskError::Paused);
            }
            self.file.write_all(&chunk).await?;
        }
        if size == 0 {
            return Err(ApiError::bad("平台返回了空文件，未保存作品").into());
        }
        self.reserve(name, size, inline_key)?;
        Ok(())
    }
    async fn into_std(mut self) -> Result<(std::fs::File, Vec<Entry>)> {
        self.file.flush().await?;
        Ok((self.file.into_std().await, self.entries))
    }
}
pub fn extension(s: &str) -> String {
    Path::new(s)
        .extension()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_ascii_lowercase()
}
pub fn supported(name: &str) -> bool {
    [
        "jpg", "jpeg", "png", "webp", "gif", "bmp", "txt", "md", "mp4", "m4v", "webm", "mov",
    ]
    .contains(&extension(name).as_str())
}
pub fn natural(a: &str, b: &str) -> std::cmp::Ordering {
    let a = a.to_lowercase();
    let b = b.to_lowercase();
    let (mut ai, mut bi) = (0, 0);
    while ai < a.len() && bi < b.len() {
        let (ad, bd) = (
            a.as_bytes()[ai].is_ascii_digit(),
            b.as_bytes()[bi].is_ascii_digit(),
        );
        let mut ae = ai + 1;
        while ae < a.len() && a.as_bytes()[ae].is_ascii_digit() == ad {
            ae += 1;
        }
        let mut be = bi + 1;
        while be < b.len() && b.as_bytes()[be].is_ascii_digit() == bd {
            be += 1;
        }
        let left = &a[ai..ae];
        let right = &b[bi..be];
        let order = if ad && bd {
            let l = left.trim_start_matches('0');
            let r = right.trim_start_matches('0');
            l.len().cmp(&r.len()).then_with(|| l.cmp(r))
        } else {
            left.cmp(right)
        };
        if !order.is_eq() {
            return order;
        }
        ai = ae;
        bi = be;
    }
    a.len().saturating_sub(ai).cmp(&b.len().saturating_sub(bi))
}
pub struct Work {
    pub remote_id: String,
    pub title: String,
    pub source_url: String,
    pub kind: String,
    pub author: String,
    pub author_id: Option<String>,
    pub description: String,
    pub language: String,
    pub tags: Vec<String>,
    pub rating: String,
    pub page_count: Option<i64>,
    pub bookmarks: Option<i64>,
    pub bookmarked: Option<bool>,
    pub published: Option<String>,
    pub series: Option<Value>,
    pub files: Staging,
    pub preview: Option<(String, Vec<u8>)>,
}
impl Work {
    pub fn new(id: &str, title: &str, kind: &str) -> Result<Self> {
        Ok(Self {
            remote_id: id.into(),
            title: title.into(),
            source_url: String::new(),
            kind: kind.into(),
            author: String::new(),
            author_id: None,
            description: String::new(),
            language: String::new(),
            tags: vec![],
            rating: "unknown".into(),
            page_count: None,
            bookmarks: None,
            bookmarked: None,
            published: None,
            series: None,
            files: Staging::new()?,
            preview: None,
        })
    }
}
pub struct Blob {
    pub name: String,
    pub path: String,
    pub digest: String,
    pub size: u64,
    pub mime: String,
    pub width: Option<u32>,
    pub height: Option<u32>,
    pub inline_key: Option<String>,
}
pub struct Prepared {
    pub assets: Vec<Blob>,
    pub preview: Option<Value>,
}
pub fn source(value: &str) -> Result<()> {
    if ["local", "pixiv", "picacg", "ehentai", "jm", "kemono"].contains(&value) {
        Ok(())
    } else {
        Err(ApiError::bad("不支持的书库来源"))
    }
}
pub fn clean_name(name: &str) -> Result<String> {
    let name: String = name
        .replace('\\', "/")
        .rsplit('/')
        .next()
        .unwrap_or("")
        .chars()
        .filter(|c| !c.is_control())
        .collect::<String>()
        .nfc()
        .collect::<String>()
        .trim()
        .to_owned();
    if name.is_empty() || name.chars().count() > 512 {
        return Err(ApiError::bad("文件名为空或过长"));
    }
    Ok(name)
}
pub fn tags(names: &[String]) -> Result<Vec<String>> {
    let mut seen = HashSet::new();
    let mut out = vec![];
    for n in names {
        let n = n.nfc().collect::<String>().trim().to_owned();
        if n.chars().count() > 191 {
            return Err(ApiError::bad("标签名称超过 191 字符"));
        }
        if !n.is_empty() && seen.insert(n.clone()) {
            out.push(n);
        }
    }
    if out.len() > 200 {
        return Err(ApiError::bad("标签数量超过 200"));
    }
    out.sort();
    Ok(out)
}
fn root(root: &Path) -> Result<PathBuf> {
    let root = root.canonicalize()?;
    if std::fs::read_to_string(root.join(".library-storage"))?.trim() != "qiye-library-v1" {
        return Err(ApiError::bad("书库存储标记缺失，请检查 NAS 挂载"));
    }
    Ok(root)
}
fn directory(root: &Path, relative: &Path) -> Result<PathBuf> {
    let mut current = root.to_path_buf();
    for c in relative.components() {
        if !matches!(c, std::path::Component::Normal(_)) {
            return Err(ApiError::bad("无效的书库存储路径"));
        }
        current.push(c);
        match std::fs::symlink_metadata(&current) {
            Ok(m) => {
                if m.file_type().is_symlink() || !m.is_dir() {
                    return Err(ApiError::bad("来源书库目录不能重定向至其他路径"));
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                match std::fs::create_dir(&current) {
                    Ok(()) => (),
                    Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => (),
                    Err(e) => return Err(e.into()),
                }
            }
            Err(e) => return Err(e.into()),
        }
        if current.canonicalize()? != current {
            return Err(ApiError::bad("来源书库目录不能重定向至其他路径"));
        }
    }
    Ok(current)
}
fn verify(path: &Path, data: &[u8], digest: &str) -> Result<()> {
    let m = std::fs::symlink_metadata(path)?;
    if !m.is_file() || m.file_type().is_symlink() || m.len() != data.len() as u64 {
        return Err(ApiError::bad("书库文件完整性校验失败"));
    }
    let mut file = std::fs::File::open(path)?;
    let mut hash = Sha256::new();
    std::io::copy(&mut file, &mut HashWriter(&mut hash))?;
    if format!("{:x}", hash.finalize()) != digest {
        return Err(ApiError::bad("书库文件完整性校验失败"));
    }
    Ok(())
}
struct HashWriter<'a>(&'a mut Sha256);
pub fn verify_file(path: &Path, digest: &str, size: u64) -> Result<()> {
    let metadata = std::fs::symlink_metadata(path)?;
    if !metadata.is_file() || metadata.file_type().is_symlink() || metadata.len() != size {
        return Err(ApiError::bad("书库文件缺失或完整性校验失败"));
    }
    let mut hash = Sha256::new();
    std::io::copy(&mut std::fs::File::open(path)?, &mut HashWriter(&mut hash))?;
    if format!("{:x}", hash.finalize()) != digest {
        return Err(ApiError::bad("书库文件完整性校验失败"));
    }
    Ok(())
}
pub async fn save_file(
    s: &AppState,
    collection: &str,
    input: &Path,
    maximum: u64,
) -> Result<(String, String, u64)> {
    s.config.writable()?;
    source(collection)?;
    let input = input.to_owned();
    let collection = collection.to_owned();
    let root_path = s.config.storage_root.clone();
    let permit = s
        .fs_limit
        .clone()
        .acquire_owned()
        .await
        .map_err(|_| ApiError::bad("文件服务不可用"))?;
    tokio::task::spawn_blocking(move || {
        let _permit = permit;
        let base = root(&root_path)?;
        let mut input = std::fs::File::open(input)?;
        let size = input.metadata()?.len();
        if size == 0 || size > maximum {
            return Err(ApiError::bad("文件为空或超过保存大小限制"));
        }
        let mut hash = Sha256::new();
        std::io::copy(
            &mut (&mut input).take(maximum + 1),
            &mut HashWriter(&mut hash),
        )?;
        let digest = format!("{:x}", hash.finalize());
        input.seek(SeekFrom::Start(0))?;
        let relative = format!(
            "collections/{collection}/blobs/{}/{}/{digest}",
            &digest[..2],
            &digest[2..4]
        );
        let dir = directory(&base, Path::new(&relative).parent().unwrap())?;
        let target = base.join(&relative);
        if target.try_exists()? {
            verify_file(&target, &digest, size)?;
            return Ok((relative, digest, size));
        }
        let mut temporary = tempfile::Builder::new()
            .prefix(".pending-")
            .tempfile_in(&dir)?;
        let copied = std::io::copy(&mut input.take(maximum + 1), &mut temporary)?;
        if copied != size {
            return Err(ApiError::bad("文件在保存期间发生变化"));
        }
        temporary.flush()?;
        temporary.as_file().sync_all()?;
        verify_file(temporary.path(), &digest, size)?;
        root(&root_path)?;
        match temporary.persist_noclobber(&target) {
            Ok(_) => (),
            Err(e) if e.error.kind() == std::io::ErrorKind::AlreadyExists => {
                verify_file(&target, &digest, size)?
            }
            Err(e) => return Err(e.error.into()),
        }
        #[cfg(unix)]
        std::fs::File::open(&dir)?.sync_all()?;
        Ok((relative, digest, size))
    })
    .await
    .map_err(|_| ApiError::bad("文件保存失败"))?
}
impl Write for HashWriter<'_> {
    fn write(&mut self, b: &[u8]) -> std::io::Result<usize> {
        self.0.update(b);
        Ok(b.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
fn save(root_path: &Path, source: &str, data: &[u8]) -> Result<(String, String)> {
    source_check(source)?;
    let base = root(root_path)?;
    let digest = format!("{:x}", Sha256::digest(data));
    let relative = format!(
        "collections/{source}/blobs/{}/{}/{digest}",
        &digest[..2],
        &digest[2..4]
    );
    let dir = directory(&base, Path::new(&relative).parent().unwrap())?;
    let target = base.join(&relative);
    if target.try_exists()? {
        verify(&target, data, &digest)?;
        return Ok((relative, digest));
    }
    let mut temporary = tempfile::Builder::new()
        .prefix(".pending-")
        .tempfile_in(&dir)?;
    temporary.write_all(data)?;
    temporary.as_file_mut().sync_all()?;
    verify(temporary.path(), data, &digest)?;
    root(root_path)?;
    match temporary.persist_noclobber(&target) {
        Ok(_) => (),
        Err(e) if e.error.kind() == std::io::ErrorKind::AlreadyExists => {
            verify(&target, data, &digest)?
        }
        Err(e) => return Err(e.error.into()),
    }
    #[cfg(unix)]
    std::fs::File::open(&dir)?.sync_all()?;
    Ok((relative, digest))
}
fn source_check(s: &str) -> Result<()> {
    source(s)
}
pub(crate) fn picture(data: &[u8]) -> Result<(image::DynamicImage, ImageFormat)> {
    let (width, height) = ImageReader::new(Cursor::new(data))
        .with_guessed_format()
        .map_err(|_| ApiError::bad("图片格式无效"))?
        .into_dimensions()
        .map_err(|_| ApiError::bad("图片尺寸无效"))?;
    if width as u64 * height as u64 > 40_000_000 {
        return Err(ApiError::bad("图片像素数超过限制"));
    }
    let mut reader = ImageReader::new(Cursor::new(data))
        .with_guessed_format()
        .map_err(|_| ApiError::bad("图片格式无效"))?;
    let format = reader
        .format()
        .ok_or_else(|| ApiError::bad("不支持的图片格式"))?;
    if ![
        ImageFormat::Jpeg,
        ImageFormat::Png,
        ImageFormat::WebP,
        ImageFormat::Gif,
        ImageFormat::Bmp,
    ]
    .contains(&format)
    {
        return Err(ApiError::bad("不支持的图片格式"));
    }
    let mut limits = image::Limits::default();
    limits.max_image_width = Some(16384);
    limits.max_image_height = Some(16384);
    limits.max_alloc = Some(192 * 1024 * 1024);
    reader.limits(limits);
    let image = reader
        .decode()
        .map_err(|_| ApiError::bad("图片损坏或像素数超过限制"))?;
    if image.width() as u64 * image.height() as u64 > 40_000_000 {
        return Err(ApiError::bad("图片像素数超过限制"));
    }
    Ok((image, format))
}
type ValidatedFile = (Vec<u8>, String, Option<u32>, Option<u32>);
fn validate(name: &str, mut data: Vec<u8>, inline: Option<String>) -> Result<ValidatedFile> {
    let ext = Path::new(name)
        .extension()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    if matches!(ext.as_str(), "txt" | "md") {
        if inline.is_some() {
            return Err(ApiError::bad("正文插图必须是图片"));
        }
        if data.starts_with(&[239, 187, 191]) {
            data.drain(..3);
        }
        let text =
            std::str::from_utf8(&data).map_err(|_| ApiError::bad("文本文件必须使用 UTF-8 编码"))?;
        if text.contains('\0') || data.len() > 16 * 1024 * 1024 {
            return Err(ApiError::bad("文本包含二进制数据或超过 16 MiB"));
        }
        return Ok((data, "text/plain".into(), None, None));
    }
    if ["mp4", "m4v", "webm", "mov"].contains(&ext.as_str()) {
        if inline.is_some() {
            return Err(ApiError::bad("正文插图必须是图片"));
        }
        let mime = video_mime(&ext, &data)?;
        return Ok((data, mime.into(), None, None));
    }
    if !["jpg", "jpeg", "png", "webp", "gif", "bmp"].contains(&ext.as_str()) {
        return Err(ApiError::bad("不支持的文件格式"));
    }
    let (img, format) = picture(&data)?;
    if let Some(key) = inline
        && !regex::Regex::new(
            r"^(?:uploadedimage:[1-9]\d{0,19}|pixivimage:[1-9]\d{0,19}(?:-[1-9]\d{0,4})?)$",
        )
        .unwrap()
        .is_match(&key)
    {
        return Err(ApiError::bad("正文插图关联无效"));
    }
    let mime = match format {
        ImageFormat::Jpeg => "image/jpeg",
        ImageFormat::Png => "image/png",
        ImageFormat::WebP => "image/webp",
        ImageFormat::Gif => "image/gif",
        _ => "image/bmp",
    };
    Ok((data, mime.into(), Some(img.width()), Some(img.height())))
}
pub fn video_mime(ext: &str, data: &[u8]) -> Result<&'static str> {
    let error = || ApiError::bad("视频容器无效或文件不完整，未保存");
    if ext == "webm" {
        let head = &data[..data.len().min(4096)];
        if data.len() <= 32
            || !head.starts_with(&[0x1a, 0x45, 0xdf, 0xa3])
            || !head.windows(7).any(|w| w == b"\x42\x82\x84webm")
            || !head.windows(4).any(|w| w == b"\x18\x53\x80\x67")
        {
            return Err(error());
        }
        return Ok("video/webm");
    }
    if !["mp4", "m4v", "mov"].contains(&ext) {
        return Err(error());
    }
    let mut offset = 0usize;
    let mut count = 0;
    let (mut moov, mut mdat, mut ftyp) = (false, false, false);
    while offset < data.len() {
        if data.len() - offset < 8 || count > 100000 {
            return Err(error());
        }
        let mut size = u32::from_be_bytes(data[offset..offset + 4].try_into().unwrap()) as u64;
        let kind = &data[offset + 4..offset + 8];
        let mut header = 8u64;
        if size == 1 {
            if data.len() - offset < 16 {
                return Err(error());
            }
            size = u64::from_be_bytes(data[offset + 8..offset + 16].try_into().unwrap());
            header = 16;
        } else if size == 0 {
            size = (data.len() - offset) as u64;
        }
        if size < header || size > (data.len() - offset) as u64 {
            return Err(error());
        }
        let payload = size - header;
        moov |= kind == b"moov" && payload > 0;
        mdat |= kind == b"mdat" && payload > 0;
        ftyp |= kind == b"ftyp" && payload >= 8;
        offset += size as usize;
        count += 1;
    }
    if !moov || !mdat || ext != "mov" && !ftyp {
        return Err(error());
    }
    Ok(if ext == "mov" {
        "video/quicktime"
    } else {
        "video/mp4"
    })
}
fn read_entry(file: &mut std::fs::File, e: &Entry) -> Result<Vec<u8>> {
    file.seek(SeekFrom::Start(e.offset))?;
    let mut bytes = vec![0; e.size as usize];
    file.read_exact(&mut bytes)?;
    Ok(bytes)
}
pub async fn prepare(
    s: &AppState,
    source: &str,
    files: Staging,
    preview: Option<(String, Vec<u8>)>,
    ctx: Option<&Context>,
) -> TaskResult<Prepared> {
    s.config.writable()?;
    source_check(source)?;
    let (mut file, entries) = files.into_std().await?;
    let source = source.to_owned();
    let path = s.config.storage_root.clone();
    let token = ctx.map(|c| c.stop.clone());
    let cpu = s
        .cpu_limit
        .clone()
        .acquire_owned()
        .await
        .map_err(|_| ApiError::bad("文件处理器已关闭"))?;
    let fs = s
        .fs_limit
        .clone()
        .acquire_owned()
        .await
        .map_err(|_| ApiError::bad("文件服务已关闭"))?;
    tokio::task::spawn_blocking(move||->TaskResult<Prepared>{let (_cpu,_fs)=(cpu,fs);root(&path)?;
        // First pass validates all entries, before the first blob is written.
        let mut checked=Vec::with_capacity(entries.len());
        for e in &entries{if token.as_ref().is_some_and(|t|t.is_cancelled()){return Err(crate::tasks::TaskError::Paused);}let (data,mime,width,height)=validate(&e.name,read_entry(&mut file,e)?,e.inline_key.clone())?;checked.push((mime,width,height,data.len()));}
        let mut assets=vec![];
        for (e,(mime,width,height,size)) in entries.iter().zip(checked){if token.as_ref().is_some_and(|t|t.is_cancelled()){return Err(crate::tasks::TaskError::Paused);}let mut data=read_entry(&mut file,e)?;if mime=="text/plain"&&data.starts_with(&[239,187,191]){data.drain(..3);}
        if data.len()!=size{return Err(ApiError::bad("暂存文件发生变化").into());}let (relative,digest)=save(&path,&source,&data)?;assets.push(Blob{name:e.name.clone(),path:relative,digest,size:data.len() as u64,mime,width,height,inline_key:e.inline_key.clone()});}
        let preview=if let Some((_,bytes))=preview.filter(|(_,b)|b.len()<=8*1024*1024){if let Ok((img,_))=picture(&bytes){let thumb=img.thumbnail(640,960).to_rgba8();let mut encoded=Vec::new();image::codecs::webp::WebPEncoder::new_lossless(&mut encoded).encode(&thumb,thumb.width(),thumb.height(),image::ExtendedColorType::Rgba8).map_err(|_|ApiError::bad("封面转换失败"))?;let (relative,digest)=save(&path,&source,&encoded)?;Some(json!({"relative_path":relative,"sha256":digest,"size_bytes":encoded.len(),"mime_type":"image/webp"}))}else{None}}else{None};
        Ok(Prepared{assets,preview})
    }).await.map_err(|_|ApiError::bad("文件处理失败"))?
}
pub async fn ensure_tag(s: &AppState, tx: &mut Tx, name: &str) -> Result<String> {
    let sql = if s.db.mysql() {
        "INSERT INTO tags(id,name) VALUES(?,?) ON DUPLICATE KEY UPDATE id=id"
    } else {
        "INSERT INTO tags(id,name) VALUES(?,?) ON CONFLICT(name) DO NOTHING"
    };
    tx.execute(sql, &[json!(uuid::Uuid::new_v4().to_string()), json!(name)])
        .await?;
    let sql = if s.db.mysql() {
        "SELECT id FROM tags WHERE name=? FOR UPDATE"
    } else {
        "SELECT id FROM tags WHERE name=?"
    };
    Ok(tx.query(sql, &[json!(name)]).await?.remove(0)["id"]
        .as_str()
        .unwrap()
        .into())
}
pub async fn set_tags(
    s: &AppState,
    tx: &mut Tx,
    id: &str,
    names: &[String],
    origin: &str,
) -> Result<()> {
    let names = tags(names)?;
    tx.execute(
        "DELETE FROM item_tags WHERE item_id=? AND origin=?",
        &[json!(id), json!(origin)],
    )
    .await?;
    for name in names {
        let tag = ensure_tag(s, tx, &name).await?;
        tx.execute("INSERT INTO item_tags(item_id,tag_id,origin,score,model_version) VALUES(?,?,?,NULL,NULL)",&[json!(id),json!(tag),json!(origin)]).await?;
    }
    Ok(())
}
pub async fn merge(s: &AppState, tx: &mut Tx, source: &str, w: &Work) -> Result<Value> {
    source_check(source)?;
    if w.title.trim().is_empty()
        || w.title.chars().count() > 512
        || !["manga", "illustration", "novel", "article"].contains(&w.kind.as_str())
    {
        return Err(ApiError::bad("作品标题或内容类型无效"));
    }
    let existing = tx
        .query(
            "SELECT * FROM items WHERE source=? AND source_id=?",
            &[json!(source), json!(w.remote_id)],
        )
        .await?
        .pop();
    let id = existing
        .as_ref()
        .map(|v| v["id"].as_str().unwrap().to_owned())
        .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
    let time = now();
    if existing.is_none() {
        tx.execute("INSERT INTO items(id,title,type,source,source_id,source_url,description,author,language,rating,rating_origin,content_state,user_score,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,'source','metadata',0,?,?)",&[json!(id),json!(w.title),json!(w.kind),json!(source),json!(w.remote_id),json!(w.source_url),json!(w.description),json!(w.author.chars().take(512).collect::<String>()),json!(w.language.chars().take(32).collect::<String>()),json!(w.rating),json!(time),json!(time)]).await?;
        set_tags(s, tx, &id, &w.tags, "source").await?;
    }
    let mut fields = vec![];
    let mut values = vec![];
    for (key, value) in [
        ("author_source_id", w.author_id.as_ref().map(|v| json!(v))),
        (
            "remote_page_count",
            w.page_count
                .filter(|n| *n >= 0 && *n <= 1_000_000)
                .map(|v| json!(v)),
        ),
        (
            "source_bookmark_count",
            w.bookmarks.filter(|n| *n >= 0).map(|v| json!(v)),
        ),
        (
            "source_published_at",
            w.published.as_ref().map(|v| json!(v)),
        ),
        (
            "source_series",
            w.series.as_ref().map(|v| json!(v.to_string())),
        ),
    ] {
        if let Some(value) = value {
            fields.push(format!("{key}=?"));
            values.push(value);
        }
    }
    if !fields.is_empty() {
        values.push(json!(time));
        values.push(json!(id));
        tx.execute(
            &format!(
                "UPDATE items SET {},updated_at=? WHERE id=?",
                fields.join(",")
            ),
            &values,
        )
        .await?;
    }
    Ok(tx
        .query("SELECT * FROM items WHERE id=?", &[json!(id)])
        .await?
        .remove(0))
}
pub async fn attach(tx: &mut Tx, id: &str, assets: &[Blob], append_only: bool) -> Result<usize> {
    let old = tx
        .query(
            "SELECT id,position,inline_key,status FROM assets WHERE item_id=? ORDER BY position",
            &[json!(id)],
        )
        .await?;
    if !old.is_empty() && !append_only {
        return Ok(0);
    }
    let mut position = old
        .iter()
        .filter_map(|v| v["position"].as_i64())
        .max()
        .unwrap_or(-1)
        + 1;
    let mut count = 0;
    for a in assets {
        if append_only && a.inline_key.is_none() {
            continue;
        }
        let previous = old.iter().find(|v| {
            a.inline_key.is_some() && v["inline_key"].as_str() == a.inline_key.as_deref()
        });
        if previous.is_some_and(|v| v["status"] == "available") {
            continue;
        }
        let id_asset = previous
            .map(|v| v["id"].as_str().unwrap().to_owned())
            .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
        let p = previous
            .and_then(|v| v["position"].as_i64())
            .unwrap_or(position);
        if previous.is_none() {
            tx.execute("INSERT INTO assets(id,item_id,position,relative_path,original_name,mime_type,sha256,size_bytes,status,inline_key,width,height) VALUES(?,?,?,?,?,?,?,?,'available',?,?,?)",&[json!(id_asset),json!(id),json!(p),json!(a.path),json!(a.name),json!(a.mime),json!(a.digest),json!(a.size),json!(a.inline_key),json!(a.width),json!(a.height)]).await?;
            position += 1;
        } else {
            tx.execute("UPDATE assets SET relative_path=?,original_name=?,mime_type=?,sha256=?,size_bytes=?,status='available',width=?,height=? WHERE id=?",&[json!(a.path),json!(a.name),json!(a.mime),json!(a.digest),json!(a.size),json!(a.width),json!(a.height),json!(id_asset)]).await?;
        }
        count += 1;
    }
    if !assets.is_empty() && !append_only {
        tx.execute(
            "UPDATE items SET content_state='ready',content_error=NULL,updated_at=? WHERE id=?",
            &[json!(now()), json!(id)],
        )
        .await?;
    }
    Ok(count)
}
