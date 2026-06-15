import FeatureGate from '@/components/layout/FeatureGate';
import FinanceShell from './FinanceShell';

export default function FinanceLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <FeatureGate flag="jess_advanced" label="Finance">
      <FinanceShell>{children}</FinanceShell>
    </FeatureGate>
  );
}
