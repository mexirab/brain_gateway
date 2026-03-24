'use client';

import { useState, useEffect, useRef } from 'react';
import { FileText, Upload, Search, Trash2, Download, X } from 'lucide-react';
import { api } from '@/lib/api';
import type { VaultDocument } from '@/lib/types';

const CATEGORIES = ['all', 'auto', 'financial', 'medical', 'legal', 'insurance', 'personal', 'housing', 'other'];

const CAT_COLORS: Record<string, string> = {
  auto: 'bg-blue-500/20 text-blue-400',
  financial: 'bg-green-500/20 text-green-400',
  medical: 'bg-red-500/20 text-red-400',
  legal: 'bg-amber-500/20 text-amber-400',
  insurance: 'bg-purple-500/20 text-purple-400',
  personal: 'bg-indigo-500/20 text-indigo-400',
  housing: 'bg-teal-500/20 text-teal-400',
  other: 'bg-zinc-500/20 text-zinc-400',
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
          <FileText size={24} className="text-indigo-400" />
          Document Vault
        </h1>
        <button
          onClick={() => setShowUpload(true)}
          className="flex items-center gap-2 px-4 py-2 rounded-lg bg-indigo-500/20 text-indigo-400 hover:bg-indigo-500/30 transition-colors text-sm font-medium"
        >
          <Upload size={16} />
          Upload
        </button>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap gap-2 mb-4">
        {CATEGORIES.map((cat) => (
          <button
            key={cat}
            onClick={() => setCategory(cat)}
            className={`px-3 py-1 rounded-full text-xs transition-colors ${
              category === cat
                ? 'bg-indigo-500/30 text-indigo-300'
                : 'bg-zinc-800/50 text-zinc-500 hover:text-zinc-300'
            }`}
          >
            {cat}
          </button>
        ))}
      </div>

      {/* Search */}
      <div className="relative mb-4">
        <Search size={16} className="absolute left-3 top-2.5 text-zinc-500" />
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search documents..."
          className="w-full pl-9 pr-4 py-2 bg-zinc-800/60 border border-zinc-700/50 rounded-lg text-sm text-white placeholder-zinc-500 focus:outline-none focus:border-indigo-500/50"
        />
      </div>

      {/* Document list */}
      <div className="flex-1 overflow-y-auto space-y-2">
        {docs.length === 0 && (
          <div className="flex-1 flex items-center justify-center h-full">
            <div className="text-center text-zinc-500">
              <FileText size={48} className="mx-auto mb-3 opacity-30" />
              <p className="text-sm">No documents yet</p>
              <p className="text-xs mt-1 text-zinc-600">Upload your first document to get started</p>
            </div>
          </div>
        )}

        {docs.map((doc) => (
          <div
            key={doc.id}
            className="flex items-center gap-3 px-4 py-3 bg-zinc-800/40 border border-zinc-700/30 rounded-lg group"
          >
            <FileText size={20} className="text-zinc-500 shrink-0" />
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium text-zinc-200 truncate">{doc.title}</p>
              <div className="flex items-center gap-2 mt-0.5">
                <span className={`text-[10px] px-1.5 py-0.5 rounded ${CAT_COLORS[doc.category] || CAT_COLORS.other}`}>
                  {doc.category}
                </span>
                <span className="text-[10px] text-zinc-600">{formatSize(doc.file_size)}</span>
                <span className="text-[10px] text-zinc-600">{formatDate(doc.uploaded_at)}</span>
                {doc.tags && <span className="text-[10px] text-zinc-600 truncate">{doc.tags}</span>}
              </div>
            </div>
            <a
              href={`/api/proxy/api/documents/${doc.id}/download`}
              className="opacity-0 group-hover:opacity-100 text-zinc-500 hover:text-indigo-400 transition-opacity"
              title="Download"
            >
              <Download size={16} />
            </a>
            <button
              onClick={() => handleDelete(doc.id)}
              className="opacity-0 group-hover:opacity-100 text-zinc-500 hover:text-red-400 transition-opacity"
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
      <div className="bg-zinc-900 border border-zinc-700 rounded-xl w-full max-w-md p-6 space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-bold">Upload Document</h2>
          <button onClick={onClose} className="text-zinc-500 hover:text-white">
            <X size={20} />
          </button>
        </div>

        {/* File input */}
        <div>
          <input ref={fileRef} type="file" onChange={handleFileChange} className="hidden" accept=".pdf,.png,.jpg,.jpeg,.tiff,.tif,.bmp,.webp,.txt,.md" />
          <button
            onClick={() => fileRef.current?.click()}
            className="w-full py-8 border-2 border-dashed border-zinc-700 rounded-lg text-zinc-500 hover:border-indigo-500/50 hover:text-indigo-400 transition-colors text-sm"
          >
            {file ? file.name : 'Click to select file'}
          </button>
        </div>

        {/* Title */}
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Document title"
          className="w-full px-3 py-2 bg-zinc-800/60 border border-zinc-700/50 rounded-lg text-sm text-white placeholder-zinc-500 focus:outline-none focus:border-indigo-500/50"
        />

        {/* Category */}
        <select
          value={category}
          onChange={(e) => setCategory(e.target.value)}
          className="w-full px-3 py-2 bg-zinc-800/60 border border-zinc-700/50 rounded-lg text-sm text-white focus:outline-none focus:border-indigo-500/50"
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
          className="w-full px-3 py-2 bg-zinc-800/60 border border-zinc-700/50 rounded-lg text-sm text-white placeholder-zinc-500 focus:outline-none focus:border-indigo-500/50"
        />

        {/* Notes */}
        <textarea
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          placeholder="Notes (optional)"
          rows={2}
          className="w-full px-3 py-2 bg-zinc-800/60 border border-zinc-700/50 rounded-lg text-sm text-white placeholder-zinc-500 focus:outline-none focus:border-indigo-500/50 resize-none"
        />

        {error && <p className="text-red-400 text-xs">{error}</p>}

        <button
          onClick={handleSubmit}
          disabled={!file || !title.trim() || uploading}
          className="w-full py-2.5 rounded-lg bg-indigo-500/20 text-indigo-400 hover:bg-indigo-500/30 transition-colors text-sm font-medium disabled:opacity-30"
        >
          {uploading ? 'Uploading...' : 'Upload'}
        </button>
      </div>
    </div>
  );
}
