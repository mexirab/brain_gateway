'use client';

import { useState, useEffect, useRef } from 'react';
import { FileText, Upload, Search, Trash2, Download, X } from 'lucide-react';
import { api } from '@/lib/api';
import { Button } from '@/components/ui';
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

  const fetchDocs = async () => {
    try {
      const cat = category === 'all' ? undefined : category;
      const results = await api.documents(cat, search || undefined);
      setDocs(results);
    } catch { /* ignore */ }
  };

  useEffect(() => { fetchDocs(); }, [category, search]);

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
    <div className="h-full flex flex-col" style={{ height: 'calc(100vh - 3rem)' }}>
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
            <div className="text-center text-content-muted">
              <FileText size={48} className="mx-auto mb-3 opacity-30" />
              <p className="text-sm">No documents yet</p>
              <p className="text-xs mt-1 text-content-muted">Upload your first document to get started</p>
            </div>
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
              className="opacity-0 group-hover:opacity-100 text-content-muted hover:text-brand transition-opacity"
              title="Download"
            >
              <Download size={16} />
            </a>
            <button
              onClick={() => handleDelete(doc.id)}
              className="opacity-0 group-hover:opacity-100 text-content-muted hover:text-danger transition-opacity"
              title="Delete"
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
  const fileRef = useRef<HTMLInputElement>(null);

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (f) {
      setFile(f);
      if (!title) setTitle(f.name.replace(/\.[^.]+$/, '').replace(/[_-]/g, ' '));
    }
  };

  const handleSubmit = async () => {
    if (!file || !title.trim()) return;
    setError('');
    setUploading(true);
    try {
      await api.uploadDocument(file, title.trim(), category, tags, notes);
      onUploaded();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed');
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
      <div className="bg-surface-base border border-line rounded-xl w-full max-w-md p-6 space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-bold">Upload Document</h2>
          <button onClick={onClose} className="text-content-muted hover:text-white">
            <X size={20} />
          </button>
        </div>

        {/* File input */}
        <div>
          <input ref={fileRef} type="file" onChange={handleFileChange} className="hidden" accept=".pdf,.png,.jpg,.jpeg,.tiff,.tif,.bmp,.webp,.txt,.md" />
          <button
            onClick={() => fileRef.current?.click()}
            className="w-full py-8 border-2 border-dashed border-line rounded-lg text-content-muted hover:border-brand/50 hover:text-brand transition-colors text-sm"
          >
            {file ? file.name : 'Click to select file'}
          </button>
        </div>

        {/* Title */}
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Document title"
          className="input w-full"
        />

        {/* Category */}
        <select
          value={category}
          onChange={(e) => setCategory(e.target.value)}
          className="input w-full"
        >
          {CATEGORIES.filter((c) => c !== 'all').map((c) => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>

        {/* Tags */}
        <input
          value={tags}
          onChange={(e) => setTags(e.target.value)}
          placeholder="Tags (comma separated)"
          className="input w-full"
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

        <Button
          variant="primary"
          onClick={handleSubmit}
          disabled={!file || !title.trim() || uploading}
          className="w-full"
        >
          {uploading ? 'Uploading...' : 'Upload'}
        </Button>
      </div>
    </div>
  );
}
