import Link from 'next/link';

export default function LandingPage() {
  return (
    <main className="min-h-screen flex flex-col items-center justify-center px-6">
      <div className="max-w-2xl text-center space-y-8">
        <h1 className="text-5xl font-bold tracking-tight">
          Meet <span className="text-brand-500">Jess</span>
        </h1>
        <p className="text-xl text-content-secondary">
          A personal AI assistant running on a local GPU cluster.
          Voice-first. ADHD-friendly. Always learning.
        </p>
        <div className="flex gap-4 justify-center">
          <Link
            href="/dashboard"
            className="px-6 py-3 bg-brand-600 hover:bg-brand-700 rounded-lg font-medium transition-colors"
          >
            Dashboard
          </Link>
          <Link
            href="/architecture"
            className="px-6 py-3 bg-surface-raised hover:bg-surface-overlay rounded-lg font-medium transition-colors border border-line"
          >
            Architecture
          </Link>
        </div>
      </div>
    </main>
  );
}
