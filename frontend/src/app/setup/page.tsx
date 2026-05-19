'use client';

import { useState } from 'react';
import type { Identity } from '@/lib/settings-api';
import Stepper from '@/components/setup/Stepper';
import WelcomeStep from '@/components/setup/WelcomeStep';
import IdentityStep from '@/components/setup/IdentityStep';
import ReviewStep from '@/components/setup/ReviewStep';

const STEPS = ['Welcome', 'Identity', 'Review'];

export default function SetupPage() {
  const [step, setStep] = useState(0);
  // Identity captured on the Identity step, handed to the Review step so the
  // summary needs no extra fetch and can't show stale (pre-edit) values.
  const [identity, setIdentity] = useState<Identity | null>(null);

  const goNext = () => setStep((s) => Math.min(s + 1, STEPS.length - 1));
  const goBack = () => setStep((s) => Math.max(s - 1, 0));

  return (
    <main className="flex min-h-screen items-center justify-center px-6 py-12">
      <div className="glass w-full max-w-xl space-y-8 p-8">
        <div className="space-y-1 text-center">
          <h1 className="text-2xl font-bold text-white">Set up Jess</h1>
          <p className="text-sm text-zinc-500">First-time configuration</p>
        </div>

        <Stepper steps={STEPS} current={step} />

        {step === 0 && <WelcomeStep onNext={goNext} />}
        {step === 1 && (
          <IdentityStep
            onNext={(saved) => {
              setIdentity(saved);
              goNext();
            }}
            onBack={goBack}
          />
        )}
        {step === 2 && <ReviewStep identity={identity} onBack={goBack} />}
      </div>
    </main>
  );
}
