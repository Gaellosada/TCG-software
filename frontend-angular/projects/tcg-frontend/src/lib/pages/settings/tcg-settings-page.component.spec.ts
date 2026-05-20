import { ComponentFixture, TestBed } from '@angular/core/testing';
import { TcgSettingsPageComponent } from './tcg-settings-page.component';
import {
  TCG_LS_KEYS,
  TcgUserSettingsService,
} from '../../services/tcg-user-settings.service';

/**
 * Unit specs for `TcgSettingsPageComponent`. Mirrors the React
 * `SettingsPage.test.jsx` cases (TC4.6–TC4.8) plus theme + chart-type
 * row coverage. The shared `TcgUserSettingsService` is provided
 * component-scoped here so each test exercises a fresh instance over
 * `localStorage`. The real service is used (not a stub) to verify the
 * page's wiring through the service is correct end-to-end.
 */
describe('TcgSettingsPageComponent', () => {
  let fixture: ComponentFixture<TcgSettingsPageComponent>;
  let component: TcgSettingsPageComponent;

  beforeEach(async () => {
    try {
      localStorage.clear();
    } catch {
      /* ignore */
    }
    await TestBed.configureTestingModule({
      imports: [TcgSettingsPageComponent],
      providers: [TcgUserSettingsService],
    }).compileComponents();

    fixture = TestBed.createComponent(TcgSettingsPageComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  afterEach(() => {
    try {
      localStorage.clear();
    } catch {
      /* ignore */
    }
  });

  it('instantiates', () => {
    expect(component).toBeTruthy();
  });

  it('renders the page title and the three setting rows', () => {
    const host: HTMLElement = fixture.nativeElement;
    expect(host.querySelector('.tcg-settings-page__title')?.textContent).toContain('Settings');
    const labels = Array.from(host.querySelectorAll('.tcg-settings-page__label')).map(
      (el) => (el.textContent ?? '').trim(),
    );
    expect(labels).toContain('Theme');
    expect(labels).toContain('Default chart');
    expect(labels).toContain('Default risk-free rate');
  });

  it('clicking the Light theme button persists "light" via the service / localStorage', () => {
    const host: HTMLElement = fixture.nativeElement;
    const lightBtn = Array.from(host.querySelectorAll<HTMLButtonElement>('button')).find(
      (b) => (b.textContent ?? '').toLowerCase().includes('light'),
    );
    expect(lightBtn).withContext('Light theme button rendered').toBeTruthy();
    lightBtn!.click();
    fixture.detectChanges();
    expect(component.theme()).toBe('light');
    expect(localStorage.getItem(TCG_LS_KEYS.theme)).toBe('light');
  });

  it('clicking the Candlestick chart button persists "candlestick" via the service', () => {
    const host: HTMLElement = fixture.nativeElement;
    const candleBtn = Array.from(host.querySelectorAll<HTMLButtonElement>('button')).find(
      (b) => (b.textContent ?? '').toLowerCase().includes('candle'),
    );
    expect(candleBtn).withContext('Candlestick button rendered').toBeTruthy();
    candleBtn!.click();
    fixture.detectChanges();
    expect(component.chartType()).toBe('candlestick');
    expect(localStorage.getItem(TCG_LS_KEYS.chartType)).toBe('candlestick');
  });

  // TC4.6 — default value when localStorage is empty
  it('renders the risk-free rate input with the service default when localStorage is empty', () => {
    const host: HTMLElement = fixture.nativeElement;
    const input = host.querySelector<HTMLInputElement>('input[type="number"]');
    expect(input).withContext('RFR input rendered').toBeTruthy();
    expect(input!.value).toBe('4'); // default '4' from TCG_DEFAULT_RISK_FREE_RATE_PCT via String(4)
  });

  // TC4.7 — persists a valid positive value
  it('persists a valid positive value to localStorage on change', () => {
    component.onRfChange('5.00');
    fixture.detectChanges();
    expect(component.rfPct()).toBe('5.00');
    expect(localStorage.getItem(TCG_LS_KEYS.riskFreeRate)).toBe('5.00');
  });

  it('persists zero to localStorage', () => {
    component.onRfChange('0');
    fixture.detectChanges();
    expect(component.rfPct()).toBe('0');
    expect(localStorage.getItem(TCG_LS_KEYS.riskFreeRate)).toBe('0');
  });

  // TC4.8 — negative value is NOT persisted, but stays in the displayed field
  it('does NOT write a negative value to localStorage', () => {
    component.onRfChange('-1');
    fixture.detectChanges();
    expect(component.rfPct()).toBe('-1');
    expect(localStorage.getItem(TCG_LS_KEYS.riskFreeRate)).not.toBe('-1');
  });

  it('does NOT write a non-numeric value to localStorage', () => {
    component.onRfChange('abc');
    fixture.detectChanges();
    expect(component.rfPct()).toBe('abc');
    expect(localStorage.getItem(TCG_LS_KEYS.riskFreeRate)).not.toBe('abc');
  });

  it('reads the existing localStorage value via the service when initialised', () => {
    // Reset and seed localStorage, then build a fresh component to verify
    // the service reads the seeded value.
    localStorage.setItem(TCG_LS_KEYS.riskFreeRate, '3.50');
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [TcgSettingsPageComponent],
      providers: [TcgUserSettingsService],
    });
    const f2 = TestBed.createComponent(TcgSettingsPageComponent);
    f2.detectChanges();
    expect(f2.componentInstance.rfPct()).toBe('3.50');
  });
});
