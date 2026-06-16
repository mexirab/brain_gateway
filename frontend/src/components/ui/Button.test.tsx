import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Button } from './Button';

describe('Button', () => {
  it('renders the primary md variant by default', () => {
    render(<Button>Save</Button>);
    expect(screen.getByRole('button', { name: 'Save' })).toHaveClass('btn', 'btn-md', 'btn-primary');
  });

  it('applies the success + icon-only variants', () => {
    render(
      <Button variant="success" icon size="sm" aria-label="Confirm">
        ✓
      </Button>,
    );
    expect(screen.getByRole('button', { name: 'Confirm' })).toHaveClass('btn-success', 'btn-icon-sm');
  });

  it('forwards clicks and merges className', async () => {
    const onClick = vi.fn();
    render(
      <Button className="extra" onClick={onClick}>
        Go
      </Button>,
    );
    const btn = screen.getByRole('button', { name: 'Go' });
    expect(btn).toHaveClass('extra');
    await userEvent.click(btn);
    expect(onClick).toHaveBeenCalledOnce();
  });

  it('does not fire onClick when disabled', async () => {
    const onClick = vi.fn();
    render(
      <Button disabled onClick={onClick}>
        Nope
      </Button>,
    );
    await userEvent.click(screen.getByRole('button', { name: 'Nope' }));
    expect(onClick).not.toHaveBeenCalled();
  });
});
