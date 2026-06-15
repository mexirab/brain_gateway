'use client';

import { useEffect, useState } from 'react';
import { Sparkles } from 'lucide-react';

interface Props {
  amount: number;
  description: string;
  onDismiss: () => void;
}

export default function XPToast({ amount, description, onDismiss }: Props) {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    // Animate in
    requestAnimationFrame(() => setVisible(true));

    // Auto-dismiss after 3 seconds
    const timer = setTimeout(() => {
      setVisible(false);
      setTimeout(onDismiss, 300); // wait for exit animation
    }, 3000);

    return () => clearTimeout(timer);
  }, [onDismiss]);

  return (
    <div
      className={`fixed top-6 right-6 z-50 transition-all duration-300 ${
        visible
          ? 'opacity-100 translate-y-0 scale-100'
          : 'opacity-0 -translate-y-4 scale-95'
      }`}
    >
      <div className="glass border border-accent-gold/30 px-5 py-3 rounded-xl shadow-lg shadow-accent-gold/10 flex items-center gap-3">
        <div className="text-accent-gold animate-bounce">
          <Sparkles size={20} />
        </div>
        <div>
          <p className="text-accent-gold font-bold text-lg">+{amount} XP</p>
          <p className="text-xs text-content-secondary">{description}</p>
        </div>
      </div>
    </div>
  );
}
