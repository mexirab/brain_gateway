import Link from 'next/link';

export default function PublicLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <>
      {/* Top nav bar */}
      <header className="sticky top-0 z-50 bg-surface/80 backdrop-blur-md border-b border-line-subtle">
        <div className="max-w-6xl mx-auto px-6 py-3 flex items-center justify-between">
          <Link href="/" className="text-lg font-bold text-brand-500">
            Jess
          </Link>
          <nav className="flex items-center gap-4">
            <Link
              href="/dashboard"
              className="text-sm text-content-secondary hover:text-white transition-colors"
            >
              Dashboard
            </Link>
            <Link
              href="/architecture"
              className="text-sm text-content-secondary hover:text-white transition-colors"
            >
              Architecture
            </Link>
            <Link
              href="/chat"
              className="text-sm text-content-secondary hover:text-white transition-colors"
            >
              Chat
            </Link>
          </nav>
        </div>
      </header>
      {children}
    </>
  );
}
