import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { TCG_API_BASE_URL } from './tcg-api.tokens';
import { TcgOptionsApi } from './tcg-options-api.service';

describe('TcgOptionsApi', () => {
  let api: TcgOptionsApi;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: TCG_API_BASE_URL, useValue: 'http://test-host:8000' },
        TcgOptionsApi,
      ],
    });
    api = TestBed.inject(TcgOptionsApi);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it('getOptionRoots() — GET /api/options/roots', () => {
    let result: unknown;
    api.getOptionRoots().subscribe((v) => (result = v));
    const req = httpMock.expectOne('http://test-host:8000/api/options/roots');
    expect(req.request.method).toBe('GET');
    req.flush({ roots: [{ name: 'OPT_SP_500', has_greeks: true }] });
    expect(result).toEqual({ roots: [{ name: 'OPT_SP_500', has_greeks: true }] });
  });

  it('getOptionExpirations() — GET /api/options/expirations?root=', () => {
    api.getOptionExpirations('OPT_SP_500').subscribe();
    const req = httpMock.expectOne(
      (r) => r.url === 'http://test-host:8000/api/options/expirations',
    );
    expect(req.request.method).toBe('GET');
    expect(req.request.params.get('root')).toBe('OPT_SP_500');
    req.flush({ root: 'OPT_SP_500', expirations: ['2026-01-16', '2026-02-20'] });
  });

  it('getOptionChain() — GET /api/options/chain with full param block', () => {
    api
      .getOptionChain('OPT_SP_500', {
        date: '2026-05-20',
        type: 'both',
        expirationMin: '2026-05-20',
        expirationMax: '2026-08-20',
        strikeMin: 4000,
        strikeMax: 5000,
        computeMissing: true,
        expirationCycle: 'M',
      })
      .subscribe();
    const req = httpMock.expectOne(
      (r) => r.url === 'http://test-host:8000/api/options/chain',
    );
    expect(req.request.method).toBe('GET');
    expect(req.request.params.get('root')).toBe('OPT_SP_500');
    expect(req.request.params.get('date')).toBe('2026-05-20');
    expect(req.request.params.get('type')).toBe('both');
    expect(req.request.params.get('expiration_min')).toBe('2026-05-20');
    expect(req.request.params.get('expiration_max')).toBe('2026-08-20');
    expect(req.request.params.get('strike_min')).toBe('4000');
    expect(req.request.params.get('strike_max')).toBe('5000');
    expect(req.request.params.get('compute_missing')).toBe('true');
    expect(req.request.params.get('expiration_cycle')).toBe('M');
    req.flush({ rows: [] });
  });

  it('getOptionChain() — omits empty expiration_cycle (mirrors React behaviour)', () => {
    api
      .getOptionChain('OPT_SP_500', {
        date: '2026-05-20',
        expirationMin: '2026-05-20',
        expirationMax: '2026-08-20',
        expirationCycle: '   ',
      })
      .subscribe();
    const req = httpMock.expectOne(
      (r) => r.url === 'http://test-host:8000/api/options/chain',
    );
    expect(req.request.params.get('expiration_cycle')).toBeNull();
    req.flush({ rows: [] });
  });

  it('getOptionChain() — omits strike bounds when null', () => {
    api
      .getOptionChain('OPT_SP_500', {
        date: '2026-05-20',
        expirationMin: '2026-05-20',
        expirationMax: '2026-08-20',
        strikeMin: null,
        strikeMax: null,
      })
      .subscribe();
    const req = httpMock.expectOne(
      (r) => r.url === 'http://test-host:8000/api/options/chain',
    );
    expect(req.request.params.get('strike_min')).toBeNull();
    expect(req.request.params.get('strike_max')).toBeNull();
    req.flush({ rows: [] });
  });

  it('getOptionContract() — encodes composite ids with | (path segment)', () => {
    api
      .getOptionContract('OPT_SP_500', 'SPY_240419C00500000|M', { computeMissing: true })
      .subscribe();
    const req = httpMock.expectOne((r) =>
      r.url.startsWith('http://test-host:8000/api/options/contract/OPT_SP_500/'),
    );
    expect(req.request.method).toBe('GET');
    expect(req.request.url).toBe(
      'http://test-host:8000/api/options/contract/OPT_SP_500/SPY_240419C00500000%7CM',
    );
    expect(req.request.params.get('compute_missing')).toBe('true');
    req.flush({ contract: { contract_id: 'SPY_240419C00500000|M' }, rows: [] });
  });

  it('getChainSnapshot() — repeats `expirations` param via append', () => {
    api
      .getChainSnapshot('OPT_SP_500', {
        date: '2026-05-20',
        type: 'C',
        expirations: ['2026-06-19', '2026-07-17'],
        field: 'iv',
        expiration_cycle: 'M',
      })
      .subscribe();
    const req = httpMock.expectOne(
      (r) => r.url === 'http://test-host:8000/api/options/chain-snapshot',
    );
    expect(req.request.params.getAll('expirations')).toEqual([
      '2026-06-19',
      '2026-07-17',
    ]);
    expect(req.request.params.get('date')).toBe('2026-05-20');
    expect(req.request.params.get('type')).toBe('C');
    expect(req.request.params.get('field')).toBe('iv');
    expect(req.request.params.get('expiration_cycle')).toBe('M');
    req.flush({ series: [] });
  });

  it('selectOption() — JSON-stringified query under ?q=', () => {
    const query = {
      root: 'OPT_SP_500',
      date: '2026-05-20',
      type: 'C',
      criterion: { kind: 'by_delta', target: 0.25 },
      maturity: { kind: 'next_third_friday' },
    };
    api.selectOption(query).subscribe();
    const req = httpMock.expectOne(
      (r) => r.url === 'http://test-host:8000/api/options/select',
    );
    expect(req.request.method).toBe('GET');
    expect(req.request.params.get('q')).toBe(JSON.stringify(query));
    req.flush({ contract_id: 'SPX240620C04500000' });
  });

  it('subscription teardown aborts in-flight request (Angular cancellation semantic)', () => {
    const sub = api.getOptionRoots().subscribe();
    const req = httpMock.expectOne('http://test-host:8000/api/options/roots');
    sub.unsubscribe();
    expect(req.cancelled).toBe(true);
  });
});
