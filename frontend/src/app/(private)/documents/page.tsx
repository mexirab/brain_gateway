'use client';

import { useState, useEffect, useCallback, useRef } from 'react';
import { FileText, Upload, Search, Trash2, Download, Camera } from 'lucide-react';
import { api } from '@/lib/api';
import { Button, Modal, Input, EmptyState } from '@/components/ui';
import { friendlyError } from '@/lib/errors';
import { resizeImage } from '@/lib/image-resize';
import type { VaultDocument } from '@/lib/types';

const CATEGORIES = ['all', 'auto', 'financial', 'medical', 'legal', 'insurance', 'personal', 'housing', 'other'];

const CAT_COLORS: Record<string, string> = {
  auto: 'bg-info/20 text-info',
  financial: 'bg-success/20 text-success',
  medical: 'bg-danger/20 text-danger',
  legal: 'bg-warning/20 text-warning',
  insurance: 'bg-brand/20 text-brand',
  personal: 'bg-brand/20 text-brand',
  housing: 'bg-success/20 text-success',
  other: 'bg-surface-overlay text-content-secondary',
};

// Camera/phone photo types that go through resize + name suggestion.
const PHOTO_TYPES = /^image\/(jpeg|png|webp|heic|heif)$/;

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

export default function DocumentsPage() {
  const [docs, setDocs] = useState<VaultDocument[]>([]);
  const [category, setCategory] = useState('all');
  const [search, setSearch] = useState('');
  const [showUpload, setShowUpload] = useState(false);
  const [uploading, setUploading] = useState(false);

  const fetchDocs = useCallback(async () => {
    try {
      const cat = category === 'all' ? undefined : category;
      const results = await api.documents(cat, search || undefined);
      setDocs(results);
    } catch { /* ignore */ }
  }, [category, search]);

  useEffect(() => { fetchDocs(); }, [fetchDocs]);

  const handleDelete = async (id: string) => {
    if (!confirm('Delete this document permanently?')) return;
    try {
      await api.deleteDocument(id);
      setDocs((prev) => prev.filter((d) => d.id !== id));
    } catch {
      /* silent — doc stays in list */
    }
  };

  return (
    <div className="page-viewport flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-2xl font-bold flex items-center gap-2">
          <FileText size={24} className="text-brand" />
          Document Vault
        </h1>
        <Button variant="primary" onClick={() => setShowUpload(true)}>
          <Upload size={16} />
          Upload
        </Button>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap gap-2 mb-4">
        {CATEGORIES.map((cat) => (
          <button
            key={cat}
            onClick={() => setCategory(cat)}
            className={`px-3 py-1 rounded-full text-xs transition-colors ${
              category === cat
                ? 'bg-brand/30 text-brand'
                : 'bg-surface-raised/50 text-content-muted hover:text-content-primary'
            }`}
          >
            {cat}
          </button>
        ))}
      </div>

      {/* Search */}
      <div className="relative mb-4">
        <Search size={16} className="absolute left-3 top-2.5 text-content-muted" />
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search documents..."
          className="input w-full pl-9"
        />
      </div>

      {/* Document list */}
      <div className="flex-1 overflow-y-auto space-y-2">
        {docs.length === 0 && (
          <div className="flex-1 flex items-center justify-center h-full">
            <EmptyState
              icon={<FileText size={48} />}
              title="No documents yet"
              description="Upload your first document to get started"
            />
          </div>
        )}

        {docs.map((doc) => (
          <div
            key={doc.id}
            className="flex items-center gap-3 px-4 py-3 bg-surface-raised/40 border border-line/30 rounded-lg group"
          >
            <FileText size={20} className="text-content-muted shrink-0" />
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium text-content-primary truncate">{doc.title}</p>
              <div className="flex items-center gap-2 mt-0.5">
                <span className={`text-[10px] px-1.5 py-0.5 rounded ${CAT_COLORS[doc.category] || CAT_COLORS.other}`}>
                  {doc.category}
                </span>
                <span className="text-[10px] text-content-muted">{formatSize(doc.file_size)}</span>
                <span className="text-[10px] text-content-muted">{formatDate(doc.uploaded_at)}</span>
                {doc.tags && <span className="text-[10px] text-content-muted truncate">{doc.tags}</span>}
              </div>
            </div>
            <a
              href={`/api/proxy/api/documents/${doc.id}/download`}
              className="sm:opacity-0 sm:group-hover:opacity-100 focus-visible:opacity-100 text-content-muted hover:text-brand transition-opacity"
              title="Download"
              aria-label={`Download ${doc.file_name}`}
            >
              <Download size={16} />
            </a>
            <button
              onClick={() => handleDelete(doc.id)}
              className="sm:opacity-0 sm:group-hover:opacity-100 focus-visible:opacity-100 text-content-muted hover:text-danger transition-opacity"
              title="Delete"
              aria-label={`Delete ${doc.file_name}`}
            >
              <Trash2 size={16} />
            </button>
          </div>
        ))}
      </div>

      {/* Upload Modal */}
      {showUpload && (
        <UploadModal
          onClose={() => setShowUpload(false)}
          onUploaded={() => { setShowUpload(false); fetchDocs(); }}
          uploading={uploading}
          setUploading={setUploading}
        />
      )}
    </div>
  );
}

function UploadModal({
  onClose,
  onUploaded,
  uploading,
  setUploading,
}: {
  onClose: () => void;
  onUploaded: () => void;
  uploading: boolean;
  setUploading: (v: boolean) => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState('');
  const [category, setCategory] = useState('other');
  const [tags, setTags] = useState('');
  const [notes, setNotes] = useState('');
  const [error, setError] = useState('');
  const [preview, setPreview] = useState<string | null>(null);
  const [preparing, setPreparing] = useState(false);
  const [suggesting, setSuggesting] = useState(false);
  const [hint, setHint] = useState('');
  const fileRef = useRef<HTMLInputElement>(null);
  const cameraRef = useRef<HTMLInputElement>(null);
  const titleRef = useRef<HTMLInputElement>(null);
  // Fields the user typed in are never overwritten by a late suggestion.
  const titleTouched = useRef(false);
  const categoryTouched = useRef(false);
  // Only the newest photo's suggestion may land; older reads are aborted so
  // they don't hold the brain's single slot.
  const pickSeq = useRef(0);
  const suggestAbort = useRef<AbortController | null>(null);

  useEffect(() => () => { if (preview) URL.revokeObjectURL(preview); }, [preview]);
  useEffect(() => () => suggestAbort.current?.abort(), []);

  const cantRead = () => {
    // Only nudge when the title is still Jess's to fill; never steal focus mid-typing.
    if (titleTouched.current) return;
    setHint("Couldn't read it — type a name.");
    if (!document.activeElement || document.activeElement === document.body) titleRef.current?.focus();
  };

  const suggestName = async (photo: Blob, seq: number) => {
    const ctrl = new AbortController();
    suggestAbort.current = ctrl;
    setSuggesting(true);
    try {
      const s = await api.suggestDocumentName(photo, ctrl.signal);
      if (seq !== pickSeq.current) return;
      if (s.ok && s.title) {
        if (!titleTouched.current) setTitle(s.title);
        if (!categoryTouched.current && s.category) setCategory(s.category);
      } else {
        cantRead();
      }
    } catch {
      if (seq === pickSeq.current && !ctrl.signal.aborted) cantRead();
    } finally {
      if (seq === pickSeq.current) setSuggesting(false);
    }
  };

  const handlePicked = async (f: File | undefined, fromCamera: boolean) => {
    if (!f) return;
    const seq = ++pickSeq.current;
    suggestAbort.current?.abort();
    setSuggesting(false);
    setError('');
    setHint('');
    setPreview(null);
    if (!categoryTouched.current) setCategory('other');
    const isHeic = /\.(heic|heif)$/i.test(f.name) || /^image\/hei[cf]$/.test(f.type);
    // TIFF/BMP etc. upload as-is (browsers can't decode them for resizing).
    if (!PHOTO_TYPES.test(f.type || '') && !isHeic) {
      setFile(f);
      if (!titleTouched.current) setTitle(f.name.replace(/\.[^.]+$/, '').replace(/[_-]/g, ' '));
      return;
    }
    // Photos: re-encode (HEIC → JPEG, strips GPS/EXIF, caps size). Never fall
    // back to uploading the original if the browser can't decode it.
    setPreparing(true);
    try {
      const jpeg = await resizeImage(f);
      if (seq !== pickSeq.current) return;
      const stamp = new Date().toISOString().slice(0, 10);
      setFile(new File([jpeg], `scan-${stamp}.jpg`, { type: 'image/jpeg' }));
      setPreview(URL.createObjectURL(jpeg));
      if (!titleTouched.current) setTitle('');
      void suggestName(jpeg, seq);
    } catch {
      if (seq === pickSeq.current) {
        setFile(null);
        setError(
          isHeic && !fromCamera
            ? "This browser can't open HEIC photos — export it as JPEG first."
            : "Couldn't open that photo. Try taking it again.",
        );
      }
    } finally {
      if (seq === pickSeq.current) setPreparing(false);
    }
  };

  const handleFileChange = (fromCamera: boolean) => (e: React.ChangeEvent<HTMLInputElement>) => {
    void handlePicked(e.target.files?.[0], fromCamera);
    e.target.value = '';
  };

  const handleSubmit = async () => {
    if (!file || !title.trim()) return;
    setError('');
    setUploading(true);
    try {
      await api.uploadDocument(file, title.trim(), category, tags, notes);
      onUploaded();
    } catch (err) {
      setError(friendlyError(err, 'Upload failed. Try again.'));
    } finally {
      setUploading(false);
    }
  };

  return (
    <Modal
      open
      onClose={onClose}
      title="Upload Document"
      footer={
        <Button
          variant="primary"
          onClick={handleSubmit}
          disabled={!file || !title.trim() || uploading || preparing}
          className="w-full"
        >
          {uploading ? 'Uploading...' : 'Upload'}
        </Button>
      }
    >
      <div className="space-y-4">
        {/* File / camera input */}
        <div className="grid grid-cols-2 gap-2">
          <input ref={fileRef} type="file" onChange={handleFileChange(false)} className="hidden" accept=".pdf,.png,.jpg,.jpeg,.tiff,.tif,.bmp,.webp,.heic,.heif,.txt,.md" />
          <input ref={cameraRef} type="file" onChange={handleFileChange(true)} className="hidden" accept="image/*" capture="environment" />
          <button
            type="button"
            onClick={() => cameraRef.current?.click()}
            disabled={preparing}
            className="flex flex-col items-center justify-center gap-1 py-6 border-2 border-dashed border-line rounded-lg text-content-muted hover:border-brand/50 hover:text-brand transition-colors text-sm disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/50"
          >
            <Camera className="w-5 h-5" />
            Take photo
          </button>
          <button
            type="button"
            onClick={() => fileRef.current?.click()}
            disabled={preparing}
            className="flex flex-col items-center justify-center gap-1 py-6 border-2 border-dashed border-line rounded-lg text-content-muted hover:border-brand/50 hover:text-brand transition-colors text-sm disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/50"
          >
            <Upload className="w-5 h-5" />
            Choose file
          </button>
        </div>
        {preparing && <p className="text-content-muted text-xs">Preparing photo…</p>}
        {preview ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={preview} alt="Document photo preview" className="w-full max-h-48 object-contain rounded-lg bg-surface-overlay" />
        ) : (
          file && <p className="text-content-secondary text-xs truncate">{file.name}</p>
        )}

        {/* Title */}
        <div>
          <Input
            ref={titleRef}
            value={title}
            onChange={(e) => {
              titleTouched.current = true;
              setHint('');
              setTitle(e.target.value);
            }}
            placeholder={suggesting ? 'Reading photo…' : 'Document title'}
            aria-label="Document title"
            aria-describedby="doc-title-status"
            maxLength={200}
          />
          <p id="doc-title-status" className={`text-xs mt-1 ${hint ? 'text-warning' : 'text-content-muted'}`} aria-live="polite">
            {hint || (suggesting && !title ? 'Jess is reading it… you can type a name instead.' : '')}
          </p>
        </div>

        {/* Category */}
        <select
          value={category}
          onChange={(e) => {
            categoryTouched.current = true;
            setCategory(e.target.value);
          }}
          aria-label="Category"
          className="input w-full"
        >
          {CATEGORIES.filter((c) => c !== 'all').map((c) => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>

        {/* Tags */}
        <Input
          value={tags}
          onChange={(e) => setTags(e.target.value)}
          placeholder="Tags (comma separated)"
        />

        {/* Notes */}
        <textarea
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          placeholder="Notes (optional)"
          rows={2}
          className="input w-full resize-none"
        />

        {error && <p className="text-danger text-xs">{error}</p>}
      </div>
    </Modal>
  );
}
