import type { RunAttachmentInput } from "../api/types";

const MAX_FILES = 4;
const MAX_BYTES = 240 * 1024;
export const TEXT_ATTACHMENT_ACCEPT = ".txt,.md,.csv,.json,.log,text/*";

function mediaType(file: File, allowImages: boolean): RunAttachmentInput["media_type"] {
  if (/\.png$/i.test(file.name) || file.type === "image/png") {
    if (allowImages) return "image/png";
    throw new Error("所选模型没有开启图片附件，请选择文本文件。");
  }
  if (/\.jpe?g$/i.test(file.name) || file.type === "image/jpeg") {
    if (allowImages) return "image/jpeg";
    throw new Error("所选模型没有开启图片附件，请选择文本文件。");
  }
  if (file.type.startsWith("text/") || /\.(txt|md|csv|json|log)$/i.test(file.name)) return "text/plain";
  throw new Error("请选择受支持的文件格式。");
}

export function checkAttachmentFiles(files: File[], allowImages: boolean): void {
  if (files.length > MAX_FILES || files.reduce((bytes, file) => bytes + file.size, 0) > MAX_BYTES) {
    throw new Error("每次最多添加 4 个附件，总大小不超过 240 KiB。");
  }
  for (const file of files) {
    if (!file.name.trim() || file.name !== file.name.trim() || [...file.name].length > 128 ||
        /[\\/:\p{C}]/u.test(file.name) || file.name === "." || file.name === "..") {
      throw new Error("附件名称必须是单个文件名，最多 128 个字符。");
    }
    mediaType(file, allowImages);
  }
}

function read(file: File): Promise<Uint8Array> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      if (!(reader.result instanceof ArrayBuffer)) reject(new Error("读取附件失败，请重新选择。"));
      else resolve(new Uint8Array(reader.result));
    };
    reader.onerror = reader.onabort = () => reject(new Error("读取附件失败，请重新选择。"));
    reader.readAsArrayBuffer(file);
  });
}

export async function encodeAttachments(files: File[], allowImages: boolean): Promise<RunAttachmentInput[]> {
  checkAttachmentFiles(files, allowImages);
  return Promise.all(files.map(async (file) => {
    const bytes = await read(file);
    const type = mediaType(file, allowImages);
    if (type === "text/plain") {
      try {
        new TextDecoder("utf-8", { fatal: true }).decode(bytes);
      } catch {
        throw new Error(`「${file.name}」无法作为 UTF-8 文本读取。`);
      }
    }
    // Keep the original bytes, including BOM and line endings. The limit above
    // bounds both the binary string and the JSON request.
    let binary = "";
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return { name: file.name, media_type: type, data_base64: btoa(binary) };
  }));
}
