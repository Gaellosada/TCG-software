import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { TCG_API_BASE_URL } from './tcg-api.tokens';
import { TcgDataApi } from './tcg-data-api.service';
import { TcgSeriesSummaryApi } from './tcg-series-summary-api.service';

describe('TcgSeriesSummaryApi', () => {
  let api: TcgSeriesSummaryApi;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: TCG_API_BASE_URL, useValue: 'http://test-host:8000' },
        TcgDataApi,
        TcgSeriesSummaryApi,
      ],
    });
    api = TestBed.inject(TcgSeriesSummaryApi);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it('extracts length + ISO date span from response', () => {
    let summary: unknown;
    api
      .getSeriesSummary({ collection: 'ETF', instrument_id: 'AAPL' })
      .subscribe((v) => (summary = v));
    const req = httpMock.expectOne(
      (r) => r.url === 'http://test-host:8000/api/data/ETF/AAPL',
    );
    req.flush({
      dates: ['2026-01-02', '2026-01-03', '2026-01-06'],
      open: [],
      high: [],
      low: [],
      close: [],
      volume: [],
    });
    expect(summary).toEqual({
      collection: 'ETF',
      instrument_id: 'AAPL',
      length: 3,
      start: '2026-01-02',
      end: '2026-01-06',
    });
  });

  it('null span when dates array is empty', () => {
    let summary: { length: number; start: string | null; end: string | null } | undefined;
    api
      .getSeriesSummary({ collection: 'ETF', instrument_id: 'XXX' })
      .subscribe((v) => (summary = v));
    httpMock
      .expectOne('http://test-host:8000/api/data/ETF/XXX')
      .flush({ dates: [] });
    expect(summary?.length).toBe(0);
    expect(summary?.start).toBeNull();
    expect(summary?.end).toBeNull();
  });

  it('caches successful summaries — second call does NOT re-fetch', () => {
    api
      .getSeriesSummary({ collection: 'ETF', instrument_id: 'AAPL' })
      .subscribe();
    httpMock
      .expectOne('http://test-host:8000/api/data/ETF/AAPL')
      .flush({ dates: ['2026-01-02'] });
    // Second subscription should not produce a new HTTP request.
    let secondLength: number | undefined;
    api
      .getSeriesSummary({ collection: 'ETF', instrument_id: 'AAPL' })
      .subscribe((v) => (secondLength = v.length));
    httpMock.expectNone('http://test-host:8000/api/data/ETF/AAPL');
    expect(secondLength).toBe(1);
  });

  it('failure evicts cache so retry re-fetches', () => {
    api
      .getSeriesSummary({ collection: 'ETF', instrument_id: 'AAPL' })
      .subscribe({ next: () => undefined, error: () => undefined });
    httpMock
      .expectOne('http://test-host:8000/api/data/ETF/AAPL')
      .error(new ProgressEvent('error'), { status: 500, statusText: 'err' });
    // Now a retry must trigger a fresh GET.
    api
      .getSeriesSummary({ collection: 'ETF', instrument_id: 'AAPL' })
      .subscribe({ next: () => undefined, error: () => undefined });
    httpMock
      .expectOne('http://test-host:8000/api/data/ETF/AAPL')
      .flush({ dates: ['2026-01-02'] });
  });

  it('converts YYYYMMDD integers to ISO strings (defensive integer branch)', () => {
    let summary: { start: string | null; end: string | null } | undefined;
    api
      .getSeriesSummary({ collection: 'FUT_ES', instrument_id: 'ESH24' })
      .subscribe((v) => (summary = v));
    httpMock
      .expectOne('http://test-host:8000/api/data/FUT_ES/ESH24')
      .flush({ dates: [20240102, 20240105] });
    expect(summary?.start).toBe('2024-01-02');
    expect(summary?.end).toBe('2024-01-05');
  });

  it('rejects invalid references', (done) => {
    api.getSeriesSummary({ collection: '', instrument_id: '' }).subscribe({
      next: () => done.fail('should have errored'),
      error: (e) => {
        expect(e).toBeTruthy();
        done();
      },
    });
  });
});
