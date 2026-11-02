// jack_rt: sonda JACK (pipewire-jack) da skill latencia-audio. Saída sempre em JSON.
//
//   jack_rt info                 cliente passivo, sem portas: buffer, taxa, biblioteca e
//                                latência informada de todas as portas de áudio
//   jack_rt watch <s>            cliente sem portas com process callback: ciclos, xruns,
//                                maior intervalo entre ciclos, trocas de buffer
//   jack_rt rt --out P --in P [--secs 6] [--level-db -20]
//                                ida e volta pelo cabo: trem de pulsos Hann em P(out),
//                                correlação em P(in), limiar 12 dB sobre o ruído medido antes
//
// GUARDA: o modo rt só aceita a saída física "...:playback_AUX2" (Out 3) e a entrada física
// "...:capture_AUX8" (Line 9) da FireStudio Tube, pelo NOME. Nunca Out 5/6. Código 4 = nenhum
// pulso voltou; 2 = uso/guarda; 3 = sem servidor JACK.
//
// Compilar (a auditoria faz sob demanda, em ~/.cache/latencia-audio/):
//   gcc -O2 -Wall -I ~/.local/include jack_rt.c -o jack_rt
//       -L ~/.local/lib/pipewire-jack -Wl,-rpath,$HOME/.local/lib/pipewire-jack -ljack -lm

#define _GNU_SOURCE
#include <dlfcn.h>
#include <jack/jack.h>
#include <math.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#define OUT_SUFFIX ":playback_AUX2"
#define IN_SUFFIX ":capture_AUX8"
#define PULSE_LEN 48            // amostras do pulso Hann (1 ms a 48 kHz)
#define NOISE_SECONDS 0.5
#define PULSE_INTERVAL_S 0.25
#define MAX_LAT_S 0.2           // janela de busca depois de cada pulso
#define THRESHOLD_DB 12.0
#define MAX_PULSES 4096

static void json_str(const char *s)
{
	putchar('"');
	for (; *s; s++) {
		unsigned char c = (unsigned char)*s;
		if (c == '"' || c == '\\')
			printf("\\%c", c);
		else if (c < 0x20)
			printf("\\u%04x", c);
		else
			putchar(c);
	}
	putchar('"');
}

static void fail(const char *mode, const char *msg, int code)
{
	printf("{\"ok\": false, \"mode\": ");
	json_str(mode);
	printf(", \"error\": ");
	json_str(msg);
	printf("}\n");
	exit(code);
}

static double now_us(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1e6 + ts.tv_nsec / 1e3;
}

static const char *jack_library(void)
{
	static char path[512] = "?";
	Dl_info info;
	if (dladdr((void *)jack_client_open, &info) && info.dli_fname)
		snprintf(path, sizeof(path), "%s", info.dli_fname);
	return path;
}

static jack_client_t *open_client(const char *mode, const char *name)
{
	jack_status_t st;
	jack_client_t *c = jack_client_open(name, JackNoStartServer, &st);
	if (!c)
		fail(mode, "sem servidor JACK (pipewire-jack não respondeu)", 3);
	return c;
}

static void port_latency(jack_client_t *c, const char *name, int *is_out, int *phys,
			 jack_latency_range_t *r)
{
	jack_port_t *p = jack_port_by_name(c, name);
	r->min = r->max = 0;
	*is_out = *phys = 0;
	if (!p)
		return;
	int fl = jack_port_flags(p);
	*is_out = !!(fl & JackPortIsOutput);   // porta de captura física = saída no JACK
	*phys = !!(fl & JackPortIsPhysical);
	jack_port_get_latency_range(p, *is_out ? JackCaptureLatency : JackPlaybackLatency, r);
}

// ---------------------------------------------------------------- info
static int cmd_info(void)
{
	jack_client_t *c = open_client("info", "latencia-audio info");
	const char **ports = jack_get_ports(c, NULL, JACK_DEFAULT_AUDIO_TYPE, 0);

	printf("{\"ok\": true, \"mode\": \"info\", \"lib\": ");
	json_str(jack_library());
	printf(", \"buffer\": %u, \"rate\": %u, \"realtime\": %d, \"ports\": [",
	       jack_get_buffer_size(c), jack_get_sample_rate(c), jack_is_realtime(c));
	for (int i = 0; ports && ports[i]; i++) {
		int is_out, phys;
		jack_latency_range_t r;
		port_latency(c, ports[i], &is_out, &phys, &r);
		printf("%s\n  {\"name\": ", i ? "," : "");
		json_str(ports[i]);
		printf(", \"dir\": \"%s\", \"physical\": %s, \"latency\": [%u, %u]}",
		       is_out ? "capture" : "playback", phys ? "true" : "false", r.min, r.max);
	}
	printf("]}\n");
	if (ports)
		jack_free(ports);
	jack_client_close(c);
	return 0;
}

// ---------------------------------------------------------------- watch
static atomic_uint w_cycles, w_xruns, w_bufchanges;
static atomic_uint w_last_buffer;
static double w_last_us, w_max_gap_us;    // só a thread de processo escreve

static int watch_process(jack_nframes_t n, void *arg)
{
	(void)arg;
	double t = now_us();
	if (w_last_us > 0 && t - w_last_us > w_max_gap_us)
		w_max_gap_us = t - w_last_us;
	w_last_us = t;
	atomic_fetch_add(&w_cycles, 1);
	atomic_store(&w_last_buffer, n);
	return 0;
}

static int watch_xrun(void *arg)
{
	(void)arg;
	atomic_fetch_add(&w_xruns, 1);
	return 0;
}

static int watch_buffer(jack_nframes_t n, void *arg)
{
	(void)arg;
	(void)n;
	atomic_fetch_add(&w_bufchanges, 1);
	w_last_us = 0;        // o intervalo da troca não conta como atraso
	return 0;
}

static int cmd_watch(double secs)
{
	jack_client_t *c = open_client("watch", "latencia-audio watch");
	jack_set_process_callback(c, watch_process, NULL);
	jack_set_xrun_callback(c, watch_xrun, NULL);
	jack_set_buffer_size_callback(c, watch_buffer, NULL);
	unsigned int buf0 = jack_get_buffer_size(c), rate = jack_get_sample_rate(c);
	if (jack_activate(c))
		fail("watch", "jack_activate falhou", 3);
	usleep((useconds_t)(secs * 1e6));
	jack_deactivate(c);
	unsigned int cycles = atomic_load(&w_cycles);
	double period_us = 1e6 * buf0 / (rate ? rate : 48000);
	printf("{\"ok\": true, \"mode\": \"watch\", \"secs\": %.1f, \"buffer\": %u, \"buffer_end\": %u, "
	       "\"rate\": %u, \"cycles\": %u, \"expected_cycles\": %.0f, \"xruns\": %u, "
	       "\"buffer_changes\": %u, \"max_gap_us\": %.0f, \"period_us\": %.0f}\n",
	       secs, buf0, atomic_load(&w_last_buffer), rate, cycles, secs * 1e6 / period_us,
	       atomic_load(&w_xruns), atomic_load(&w_bufchanges), w_max_gap_us, period_us);
	jack_client_close(c);
	return 0;
}

// ---------------------------------------------------------------- rt
static jack_port_t *rt_out, *rt_in;
static float *rt_rec;                 // captura gravada (só a memória, nunca em disco)
static size_t rt_rec_len;
static atomic_size_t rt_frame;        // frames processados
static float rt_pulse[PULSE_LEN];
static size_t rt_noise_frames, rt_interval, rt_tail;
static atomic_uint rt_xruns;
static atomic_int rt_bufchange;

static int rt_process(jack_nframes_t n, void *arg)
{
	(void)arg;
	float *out = jack_port_get_buffer(rt_out, n);
	const float *in = jack_port_get_buffer(rt_in, n);
	size_t f0 = atomic_load(&rt_frame);

	for (jack_nframes_t i = 0; i < n; i++) {
		size_t f = f0 + i;
		float v = 0.f;
		if (f >= rt_noise_frames && f + PULSE_LEN + rt_tail < rt_rec_len) {
			size_t k = (f - rt_noise_frames) % rt_interval;
			if (k < PULSE_LEN)
				v = rt_pulse[k];
		}
		out[i] = v;
		if (f < rt_rec_len)
			rt_rec[f] = in[i];
	}
	atomic_store(&rt_frame, f0 + n);
	return 0;
}

static int rt_xrun(void *arg)
{
	(void)arg;
	atomic_fetch_add(&rt_xruns, 1);
	return 0;
}

static int rt_buffer(jack_nframes_t n, void *arg)
{
	(void)arg;
	(void)n;
	atomic_store(&rt_bufchange, 1);
	return 0;
}

static int ends_with(const char *s, const char *suf)
{
	size_t a = strlen(s), b = strlen(suf);
	return a >= b && strcmp(s + a - b, suf) == 0;
}

static double corr_at(const float *x, size_t pos)
{
	double s = 0;
	for (int k = 0; k < PULSE_LEN; k++)
		s += x[pos + k] * rt_pulse[k];
	return s;
}

static int cmp_double(const void *a, const void *b)
{
	double x = *(const double *)a, y = *(const double *)b;
	return (x > y) - (x < y);
}

static int cmd_rt(const char *out_name, const char *in_name, double secs, double level_db)
{
	if (!out_name || !in_name)
		fail("rt", "uso: jack_rt rt --out <porta playback_AUX2> --in <porta capture_AUX8>", 2);
	// Guarda pelo nome: só Out 3 -> Line 9 da FireStudio Tube (nunca Out 5/6).
	if (!ends_with(out_name, OUT_SUFFIX) || !ends_with(in_name, IN_SUFFIX) ||
	    !strstr(out_name, "FireStudio Tube") || !strstr(in_name, "FireStudio Tube") ||
	    strstr(out_name, "Loopback") || strstr(in_name, "Loopback"))
		fail("rt", "guarda: só 'FireStudio Tube...:playback_AUX2' -> 'FireStudio Tube...:capture_AUX8'", 2);
	if (secs < 2 || secs > 30 || level_db > -6 || level_db < -60)
		fail("rt", "--secs 2..30 e --level-db -60..-6", 2);

	jack_client_t *c = open_client("rt", "latencia-audio rt");
	int is_out, phys_o, phys_i;
	jack_latency_range_t lat_out, lat_in;
	port_latency(c, out_name, &is_out, &phys_o, &lat_out);
	if (!phys_o || is_out)
		fail("rt", "porta de saída não existe ou não é física de playback", 2);
	port_latency(c, in_name, &is_out, &phys_i, &lat_in);
	if (!phys_i || !is_out)
		fail("rt", "porta de entrada não existe ou não é física de captura", 2);

	unsigned int rate = jack_get_sample_rate(c), buffer = jack_get_buffer_size(c);
	double amp = pow(10.0, level_db / 20.0);
	for (int k = 0; k < PULSE_LEN; k++)
		rt_pulse[k] = (float)(amp * 0.5 * (1 - cos(2 * M_PI * k / (PULSE_LEN - 1))));
	rt_noise_frames = (size_t)(NOISE_SECONDS * rate);
	rt_interval = (size_t)(PULSE_INTERVAL_S * rate);
	rt_tail = (size_t)(2 * MAX_LAT_S * rate);
	rt_rec_len = (size_t)((secs + NOISE_SECONDS + 2 * MAX_LAT_S) * rate);
	rt_rec = calloc(rt_rec_len + PULSE_LEN, sizeof(float));
	if (!rt_rec)
		fail("rt", "sem memória", 3);
	mlockall(MCL_CURRENT | MCL_FUTURE);

	rt_out = jack_port_register(c, "out", JACK_DEFAULT_AUDIO_TYPE, JackPortIsOutput, 0);
	rt_in = jack_port_register(c, "in", JACK_DEFAULT_AUDIO_TYPE, JackPortIsInput, 0);
	if (!rt_out || !rt_in)
		fail("rt", "não registrou as portas", 3);
	jack_set_process_callback(c, rt_process, NULL);
	jack_set_xrun_callback(c, rt_xrun, NULL);
	jack_set_buffer_size_callback(c, rt_buffer, NULL);
	if (jack_activate(c))
		fail("rt", "jack_activate falhou", 3);
	if (jack_connect(c, jack_port_name(rt_out), out_name) ||
	    jack_connect(c, in_name, jack_port_name(rt_in))) {
		jack_deactivate(c);
		jack_client_close(c);
		fail("rt", "não conectou às portas da placa", 3);
	}
	// Espera os links valerem antes de contar (o trem só começa após NOISE_SECONDS).
	double t_end = now_us() + (secs + NOISE_SECONDS + 2 * MAX_LAT_S + 3) * 1e6;
	while (atomic_load(&rt_frame) < rt_rec_len && now_us() < t_end)
		usleep(20000);
	jack_deactivate(c);
	size_t got = atomic_load(&rt_frame);
	if (got > rt_rec_len)
		got = rt_rec_len;

	// Ruído: correlação no trecho sem pulsos (pula o 1.º quantum, ainda sem link).
	double noise = 0;
	size_t nz0 = buffer * 4 < rt_noise_frames / 2 ? buffer * 4 : rt_noise_frames / 2;
	for (size_t p = nz0; p + PULSE_LEN < rt_noise_frames && p + PULSE_LEN < got; p++) {
		double v = fabs(corr_at(rt_rec, p));
		if (v > noise)
			noise = v;
	}
	double self = 0;
	for (int k = 0; k < PULSE_LEN; k++)
		self += rt_pulse[k] * rt_pulse[k];
	double thr = noise * pow(10.0, THRESHOLD_DB / 20.0);
	if (thr < self * 1e-4)
		thr = self * 1e-4;   // piso absoluto (-40 dB do pulso) quando a linha é muito limpa

	static double lat[MAX_PULSES];
	size_t nfound = 0, nsent = 0, maxlat = (size_t)(MAX_LAT_S * rate);
	double peak_ratio_min = 1e9;
	for (size_t t0 = rt_noise_frames; t0 + maxlat + PULSE_LEN < got && nsent < MAX_PULSES;
	     t0 += rt_interval) {
		nsent++;
		double best = 0;
		size_t bi = 0;
		for (size_t d = 0; d < maxlat; d++) {
			double v = fabs(corr_at(rt_rec, t0 + d));   // aceita polaridade invertida
			if (v > best) {
				best = v;
				bi = d;
			}
		}
		if (best < thr)
			continue;
		double frac = 0;
		if (bi > 0 && bi + 1 < maxlat) {
			double a = fabs(corr_at(rt_rec, t0 + bi - 1)), b = best, cc = fabs(corr_at(rt_rec, t0 + bi + 1));
			double den = a - 2 * b + cc;
			if (den != 0)
				frac = 0.5 * (a - cc) / den;
		}
		lat[nfound++] = bi + frac;
		if (best / (noise > 0 ? noise : 1e-12) < peak_ratio_min)
			peak_ratio_min = best / (noise > 0 ? noise : 1e-12);
	}
	jack_client_close(c);

	double med = 0, lo = 0, hi = 0;
	if (nfound) {
		qsort(lat, nfound, sizeof(double), cmp_double);
		med = lat[nfound / 2];
		lo = lat[0];
		hi = lat[nfound - 1];
	}
	printf("{\"ok\": %s, \"mode\": \"rt\", \"out\": ", nfound ? "true" : "false");
	json_str(out_name);
	printf(", \"in\": ");
	json_str(in_name);
	printf(", \"rate\": %u, \"buffer\": %u, \"level_db\": %.1f, \"pulses\": %zu, \"found\": %zu, "
	       "\"median\": %.2f, \"min\": %.2f, \"max\": %.2f, \"jitter\": %.2f, "
	       "\"noise_corr\": %.3g, \"threshold_db\": %.1f, \"snr_min_db\": %.1f, "
	       "\"informed_in\": %u, \"informed_out\": %u, \"informed\": %u, "
	       "\"xruns\": %u, \"buffer_changed\": %s}\n",
	       rate, buffer, level_db, nsent, nfound, med, lo, hi, hi - lo, noise, THRESHOLD_DB,
	       nfound ? 20 * log10(peak_ratio_min) : 0.0, lat_in.max, lat_out.max,
	       lat_in.max + lat_out.max, atomic_load(&rt_xruns),
	       atomic_load(&rt_bufchange) ? "true" : "false");
	free(rt_rec);
	return nfound ? 0 : 4;
}

int main(int argc, char **argv)
{
	signal(SIGPIPE, SIG_IGN);
	if (argc < 2)
		fail("?", "uso: jack_rt info | watch <s> | rt --out P --in P [--secs S] [--level-db D]", 2);
	if (!strcmp(argv[1], "info"))
		return cmd_info();
	if (!strcmp(argv[1], "watch")) {
		double s = argc > 2 ? atof(argv[2]) : 10;
		if (s <= 0 || s > 3600)
			fail("watch", "segundos 0..3600", 2);
		return cmd_watch(s);
	}
	if (!strcmp(argv[1], "rt")) {
		const char *o = NULL, *i = NULL;
		double secs = 6, level = -20;
		for (int a = 2; a + 1 < argc; a += 2) {
			if (!strcmp(argv[a], "--out"))
				o = argv[a + 1];
			else if (!strcmp(argv[a], "--in"))
				i = argv[a + 1];
			else if (!strcmp(argv[a], "--secs"))
				secs = atof(argv[a + 1]);
			else if (!strcmp(argv[a], "--level-db"))
				level = atof(argv[a + 1]);
			else
				fail("rt", "opção desconhecida", 2);
		}
		return cmd_rt(o, i, secs, level);
	}
	fail(argv[1], "modo desconhecido (info | watch | rt)", 2);
	return 2;
}
