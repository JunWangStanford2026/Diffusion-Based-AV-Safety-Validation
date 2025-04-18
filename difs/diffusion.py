from typing import Optional
import math
from functools import partial
import numpy as np
import torch
from torch import nn
import torch.nn.functional as F
from torch.cuda.amp import autocast
from einops import reduce
from tqdm.auto import tqdm
from difs.utils import default, identity, ModelPrediction


def normalize_to_neg_one_to_one(img):
    return img * 2 - 1

def unnormalize_to_zero_to_one(t):
    return (t + 1) * 0.5

def cosine_t2alpha_cumprod(t):
    return torch.cos((t + 0.008)/1.008 * np.pi/2)**2 / (torch.cos((torch.tensor(0, device=t.device) + 0.008)/1.008 * np.pi/2)**2)   

def extract(a, t, x_shape):
    b, *_ = t.shape
    out = a.gather(-1, t)
    return out.reshape(b, *((1,) * (len(x_shape) - 1)))

def linear_beta_schedule(timesteps):
    scale = 1000 / timesteps
    beta_start = scale * 0.0001
    beta_end = scale * 0.02
    return torch.linspace(beta_start, beta_end, timesteps, dtype=torch.float64)

def cosine_beta_schedule(timesteps, s=0.008):
    """
    cosine schedule
    as proposed in https://openreview.net/forum?id=-NEXDKk8gZ
    """
    steps = timesteps + 1
    x = torch.linspace(0, timesteps, steps, dtype=torch.float64)
    alphas_cumprod = torch.cos(((x / timesteps) + s) / (1 + s) * math.pi * 0.5) ** 2
    alphas_cumprod = alphas_cumprod / alphas_cumprod[0]
    betas = 1 - (alphas_cumprod[1:] / alphas_cumprod[:-1])
    return torch.clip(betas, 0, 0.999)

class GaussianDiffusionConditional(nn.Module):
    def __init__(
        self,
        model: nn.Module,
        seq_length: int,
        classifier_free_guidance: bool,
        *,
        timesteps: int = 1000,
        sampling_timesteps: Optional[int] = None,
        objective: str = 'pred_v',
        beta_schedule: str = 'cosine',
        ddim_sampling_eta: float = 0.,
        auto_normalize: bool = False,
        clip_min: float = -5.,
        clip_max: float = 5.,
        cfg_drop_prob: float = 0.1,
        # guidance scale typically between 1 and 10
        cfg_guidance_scale: float = 1.0
    ):
        super().__init__()
        self.model = model
        self.channels = self.model.channels
        self.cond_dim = self.model.cond_dim
        self.self_condition = False

        self.seq_length = seq_length

        self.objective = objective
        self.classifier_free_guidance = classifier_free_guidance
        self.drop_prob = cfg_drop_prob
        self.guidance_scale = cfg_guidance_scale

        assert objective in {'pred_noise', 'pred_x0', 'pred_v'}, 'objective must be either pred_noise (predict noise) or pred_x0 (predict image start) or pred_v (predict v [v-parameterization as defined in appendix D of progressive distillation paper, used in imagen-video successfully])'
        
        self.beta_schedule = beta_schedule
        if beta_schedule == 'linear':
            betas = linear_beta_schedule(timesteps)
        elif beta_schedule == 'cosine':
            betas = cosine_beta_schedule(timesteps)
        else:
            raise ValueError(f'unknown beta schedule {beta_schedule}')

        alphas = 1. - betas
        alphas_cumprod = torch.cumprod(alphas, dim=0)
        alphas_cumprod_prev = F.pad(alphas_cumprod[:-1], (1, 0), value=1.)

        timesteps, = betas.shape
        self.num_timesteps = int(timesteps)

        # sampling related parameters
        self.sampling_timesteps = default(sampling_timesteps, timesteps) # default num sampling timesteps to number of timesteps at training

        assert self.sampling_timesteps <= timesteps
        self.is_ddim_sampling = self.sampling_timesteps < timesteps
        self.ddim_sampling_eta = ddim_sampling_eta

        # helper function to register buffer from float64 to float32
        register_buffer = lambda name, val: self.register_buffer(name, val.to(torch.float32))

        register_buffer('betas', betas)
        register_buffer('alphas_cumprod', alphas_cumprod)
        register_buffer('alphas_cumprod_prev', alphas_cumprod_prev)

        # calculations for diffusion q(x_t | x_{t-1}) and others
        register_buffer('sqrt_alphas_cumprod', torch.sqrt(alphas_cumprod))
        register_buffer('sqrt_one_minus_alphas_cumprod', torch.sqrt(1. - alphas_cumprod))
        register_buffer('log_one_minus_alphas_cumprod', torch.log(1. - alphas_cumprod))
        register_buffer('sqrt_recip_alphas_cumprod', torch.sqrt(1. / alphas_cumprod))
        register_buffer('sqrt_recipm1_alphas_cumprod', torch.sqrt(1. / alphas_cumprod - 1))

        # calculations for posterior q(x_{t-1} | x_t, x_0)
        posterior_variance = betas * (1. - alphas_cumprod_prev) / (1. - alphas_cumprod)
        # above: equal to 1. / (1. / (1. - alpha_cumprod_tm1) + alpha_t / beta_t)

        register_buffer('posterior_variance', posterior_variance)

        # below: log calculation clipped because the posterior variance is 0 at the beginning of the diffusion chain
        register_buffer('posterior_log_variance_clipped', torch.log(posterior_variance.clamp(min =1e-20)))
        register_buffer('posterior_mean_coef1', betas * torch.sqrt(alphas_cumprod_prev) / (1. - alphas_cumprod))
        register_buffer('posterior_mean_coef2', (1. - alphas_cumprod_prev) * torch.sqrt(alphas) / (1. - alphas_cumprod))

        # calculate loss weight
        snr = alphas_cumprod / (1 - alphas_cumprod)

        if objective == 'pred_noise':
            loss_weight = torch.ones_like(snr)
        elif objective == 'pred_x0':
            loss_weight = snr
        elif objective == 'pred_v':
            loss_weight = snr / (snr + 1)

        register_buffer('loss_weight', loss_weight)

        # whether to autonormalize
        self.normalize = normalize_to_neg_one_to_one if auto_normalize else identity
        self.unnormalize = unnormalize_to_zero_to_one if auto_normalize else identity
        self.clip_min = clip_min
        self.clip_max = clip_max

    def predict_start_from_noise(self, x_t, t, noise):
        return (
            extract(self.sqrt_recip_alphas_cumprod, t, x_t.shape) * x_t -
            extract(self.sqrt_recipm1_alphas_cumprod, t, x_t.shape) * noise
        )

    def predict_noise_from_start(self, x_t, t, x0):
        return (
            (extract(self.sqrt_recip_alphas_cumprod, t, x_t.shape) * x_t - x0) / \
            extract(self.sqrt_recipm1_alphas_cumprod, t, x_t.shape)
        )

    def predict_v(self, x_start, t, noise):
        return (
            extract(self.sqrt_alphas_cumprod, t, x_start.shape) * noise -
            extract(self.sqrt_one_minus_alphas_cumprod, t, x_start.shape) * x_start
        )

    def predict_start_from_v(self, x_t, t, v):
        return (
            extract(self.sqrt_alphas_cumprod, t, x_t.shape) * x_t -
            extract(self.sqrt_one_minus_alphas_cumprod, t, x_t.shape) * v
        )

    def q_posterior(self, x_start, x_t, t):
        posterior_mean = (
            extract(self.posterior_mean_coef1, t, x_t.shape) * x_start +
            extract(self.posterior_mean_coef2, t, x_t.shape) * x_t
        )
        posterior_variance = extract(self.posterior_variance, t, x_t.shape)
        posterior_log_variance_clipped = extract(self.posterior_log_variance_clipped, t, x_t.shape)
        return posterior_mean, posterior_variance, posterior_log_variance_clipped

    def model_predictions(self, x, t, cond, inits, cond_scale=1., rescaled_phi=0.0, 
                            clip_x_start=False, rederive_pred_noise = False):
        model_output = self.model.forward_with_cond_scale(x, t, cond, inits, cond_scale=cond_scale, rescaled_phi=rescaled_phi)
        
        if self.classifier_free_guidance:
            model_output_unguided = self.model.forward_with_cond_scale(x, t, torch.zeros_like(cond), torch.zeros_like(inits),
                                                                        cond_scale=cond_scale, rescaled_phi=rescaled_phi)
            model_output = model_output_unguided + self.guidance_scale * (model_output - model_output_unguided)
        
        maybe_clip = partial(torch.clamp, min = self.clip_min, max = self.clip_max) if clip_x_start else identity

        if self.objective == 'pred_noise':
            pred_noise = model_output
            x_start = self.predict_start_from_noise(x, t, pred_noise)
            x_start = maybe_clip(x_start)

            if clip_x_start and rederive_pred_noise:
                pred_noise = self.predict_noise_from_start(x, t, x_start)

        elif self.objective == 'pred_x0':
            x_start = model_output
            x_start = maybe_clip(x_start)
            pred_noise = self.predict_noise_from_start(x, t, x_start)

        elif self.objective == 'pred_v':
            v = model_output
            x_start = self.predict_start_from_v(x, t, v)
            x_start = maybe_clip(x_start)
            pred_noise = self.predict_noise_from_start(x, t, x_start)

        return ModelPrediction(pred_noise, x_start)

    def p_mean_variance(self, x, t, cond, inits, cond_scale, rescaled_phi, clip_denoised=True):
        preds = self.model_predictions(x, t, cond, inits, cond_scale, rescaled_phi,)
        x_start = preds.pred_x_start

        if clip_denoised:
            x_start.clamp_(self.clip_min, self.clip_max)

        model_mean, posterior_variance, posterior_log_variance = self.q_posterior(x_start = x_start, x_t = x, t = t)
        return model_mean, posterior_variance, posterior_log_variance, x_start

    @torch.no_grad()
    def p_sample(self, x, t: int, cond, inits, cond_scale=1., rescaled_phi=0.0, clip_denoised=True):
        b, *_, device = *x.shape, x.device
        batched_times = torch.full((b,), t, device = x.device, dtype = torch.long)
        model_mean, _, model_log_variance, x_start = self.p_mean_variance(x=x, t=batched_times, cond=cond, inits=inits, cond_scale=cond_scale, rescaled_phi=rescaled_phi, clip_denoised=clip_denoised)
        noise = torch.randn_like(x) if t > 0 else 0. # no noise if t == 0
        pred_img = model_mean + (0.5 * model_log_variance).exp() * noise
        return pred_img, x_start

    @torch.no_grad()
    def p_sample_loop(self, shape, cond, inits, cond_scale=1., rescaled_phi=0.0):
        batch, device = shape[0], self.betas.device

        img = torch.randn(shape, device=device)

        for t in tqdm(reversed(range(0, self.num_timesteps)), desc='sampling loop time step', total=self.num_timesteps):
            img, x_start = self.p_sample(img, t, cond, inits, cond_scale, rescaled_phi)

        img = self.unnormalize(img)
        return img

    @torch.no_grad()
    def ddim_sample(self, shape, cond, inits, cond_scale=1., rescaled_phi=0.0, clip_denoised=True):
        batch, device, total_timesteps, sampling_timesteps, eta, objective = shape[0], self.betas.device, self.num_timesteps, self.sampling_timesteps, self.ddim_sampling_eta, self.objective

        times = torch.linspace(-1, total_timesteps - 1, steps=sampling_timesteps + 1)   # [-1, 0, 1, 2, ..., T-1] when sampling_timesteps == total_timesteps
        times = list(reversed(times.int().tolist()))
        time_pairs = list(zip(times[:-1], times[1:])) # [(T-1, T-2), (T-2, T-3), ..., (1, 0), (0, -1)]

        img = torch.randn(shape, device=device)

        x_start = None

        for time, time_next in tqdm(time_pairs, desc='sampling loop time step'):
            time_cond = torch.full((batch,), time, device=device, dtype=torch.long)

            pred_noise, x_start, *_ = self.model_predictions(img, time_cond, cond, inits, cond_scale, rescaled_phi, clip_x_start = clip_denoised)

            if time_next < 0:
                img = x_start
                continue

            alpha = self.alphas_cumprod[time]
            alpha_next = self.alphas_cumprod[time_next]

            sigma = eta * ((1 - alpha / alpha_next) * (1 - alpha_next) / (1 - alpha)).sqrt()
            c = (1 - alpha_next - sigma ** 2).sqrt()

            noise = torch.randn_like(img)

            img = x_start * alpha_next.sqrt() + \
                  c * pred_noise + \
                  sigma * noise

        img = self.unnormalize(img)
        return img

    @torch.no_grad()
    def sample(self, cond, no_grad=True, inits=None, cond_scale=1., rescaled_phi=0.0):
        batch_size = cond.shape[0]
        seq_length, channels = self.seq_length, self.channels
        sample_fn = self.p_sample_loop if not self.is_ddim_sampling else self.ddim_sample
        return sample_fn((batch_size, channels, seq_length), cond, inits, cond_scale, rescaled_phi)

    @torch.no_grad()
    def interpolate(self, x1, x2, t=None, lam=0.5):
        b, *_, device = *x1.shape, x1.device
        t = default(t, self.num_timesteps - 1)

        assert x1.shape == x2.shape

        t_batched = torch.full((b,), t, device=device)
        xt1, xt2 = map(lambda x: self.q_sample(x, t=t_batched), (x1, x2))

        img = (1 - lam) * xt1 + lam * xt2

        x_start = None

        for i in tqdm(reversed(range(0, t)), desc='interpolation sample time step', total=t):
            self_cond = x_start if self.self_condition else None
            img, x_start = self.p_sample(img, i, self_cond)

        return img

    @autocast(enabled=False)
    def q_sample(self, x_start, t, noise=None):
        noise = default(noise, lambda: torch.randn_like(x_start))

        return (
            extract(self.sqrt_alphas_cumprod, t, x_start.shape) * x_start +
            extract(self.sqrt_one_minus_alphas_cumprod, t, x_start.shape) * noise
        )

    def p_losses(self, x_start, t, cond, inits, noise=None):
        b, c, n = x_start.shape
        noise = default(noise, lambda: torch.randn_like(x_start))

        # Randomly drop conditioning for classifier-free guidance
        if self.classifier_free_guidance:
            cond_mask = torch.rand(cond.shape, device=x_start.device) > self.drop_prob
            cond = cond * cond_mask  # Mask out conditioning vectors randomly
            inits_mask = torch.rand(inits.shape, device=x_start.device) > self.drop_prob
            inits = inits * inits_mask

        # noise sample
        x = self.q_sample(x_start=x_start, t=t, noise=noise)

        # predict and take gradient step
        model_out = self.model(x, t, cond, inits)

        if self.objective == 'pred_noise':
            target = noise
        elif self.objective == 'pred_x0':
            target = x_start
        elif self.objective == 'pred_v':
            v = self.predict_v(x_start, t, noise)
            target = v
        else:
            raise ValueError(f'unknown objective {self.objective}')

        loss = F.mse_loss(model_out, target, reduction='none')
        loss = reduce(loss, 'b ... -> b', 'mean')

        loss = loss * extract(self.loss_weight, t, loss.shape)
        return loss.mean()

    def forward(self, img, *args, **kwargs):
        b, c, n, device, seq_length, = *img.shape, img.device, self.seq_length

        assert n == seq_length, f'seq length must be {seq_length}'
        t = torch.randint(0, self.num_timesteps, (b,), device=device).long()

        img = self.normalize(img)
        return self.p_losses(img, t, *args, **kwargs)




class GaussianDiffusionConditionalTrainer(nn.Module):
    def __init__(
        self,
        model: nn.Module,
        seq_length: int,
        *,
        timesteps: int = 1000,
        sampling_timesteps: Optional[int] = None,
        objective: str = 'pred_v',
        beta_schedule: str = 'cosine',
        ddim_sampling_eta: float = 0.,
        auto_normalize: bool = False,
        clip_min: float = -5.,
        clip_max: float = 5.,
        classifier_free_guidance: bool = False,
        cfg_guidance_scale: float = 1.0
    ):
        super().__init__()
        self.model = model
        self.channels = self.model.channels
        self.cond_dim = self.model.cond_dim
        self.self_condition = False

        self.seq_length = seq_length

        self.objective = objective
        self.classifier_free_guidance = classifier_free_guidance
        self.guidance_scale = cfg_guidance_scale

        assert objective in {'pred_noise', 'pred_x0', 'pred_v'}, 'objective must be either pred_noise (predict noise) or pred_x0 (predict image start) or pred_v (predict v [v-parameterization as defined in appendix D of progressive distillation paper, used in imagen-video successfully])'
        
        self.beta_schedule = beta_schedule
        if beta_schedule == 'linear':
            betas = linear_beta_schedule(timesteps)
        elif beta_schedule == 'cosine':
            betas = cosine_beta_schedule(timesteps)
        else:
            raise ValueError(f'unknown beta schedule {beta_schedule}')

        alphas = 1. - betas
        alphas_cumprod = torch.cumprod(alphas, dim=0)
        alphas_cumprod_prev = F.pad(alphas_cumprod[:-1], (1, 0), value=1.)

        timesteps, = betas.shape
        self.num_timesteps = int(timesteps)

        # sampling related parameters
        self.sampling_timesteps = default(sampling_timesteps, timesteps) # default num sampling timesteps to number of timesteps at training

        assert self.sampling_timesteps <= timesteps
        self.is_ddim_sampling = self.sampling_timesteps < timesteps
        self.ddim_sampling_eta = ddim_sampling_eta

        # helper function to register buffer from float64 to float32
        register_buffer = lambda name, val: self.register_buffer(name, val.to(torch.float32))

        register_buffer('betas', betas)
        register_buffer('alphas_cumprod', alphas_cumprod)
        register_buffer('alphas_cumprod_prev', alphas_cumprod_prev)

        # calculations for diffusion q(x_t | x_{t-1}) and others
        register_buffer('sqrt_alphas_cumprod', torch.sqrt(alphas_cumprod))
        register_buffer('sqrt_one_minus_alphas_cumprod', torch.sqrt(1. - alphas_cumprod))
        register_buffer('log_one_minus_alphas_cumprod', torch.log(1. - alphas_cumprod))
        register_buffer('sqrt_recip_alphas_cumprod', torch.sqrt(1. / alphas_cumprod))
        register_buffer('sqrt_recipm1_alphas_cumprod', torch.sqrt(1. / alphas_cumprod - 1))

        # calculations for posterior q(x_{t-1} | x_t, x_0)
        posterior_variance = betas * (1. - alphas_cumprod_prev) / (1. - alphas_cumprod)
        # above: equal to 1. / (1. / (1. - alpha_cumprod_tm1) + alpha_t / beta_t)

        register_buffer('posterior_variance', posterior_variance)

        # below: log calculation clipped because the posterior variance is 0 at the beginning of the diffusion chain
        register_buffer('posterior_log_variance_clipped', torch.log(posterior_variance.clamp(min =1e-20)))
        register_buffer('posterior_mean_coef1', betas * torch.sqrt(alphas_cumprod_prev) / (1. - alphas_cumprod))
        register_buffer('posterior_mean_coef2', (1. - alphas_cumprod_prev) * torch.sqrt(alphas) / (1. - alphas_cumprod))

        # calculate loss weight
        snr = alphas_cumprod / (1 - alphas_cumprod)

        if objective == 'pred_noise':
            loss_weight = torch.ones_like(snr)
        elif objective == 'pred_x0':
            loss_weight = snr
        elif objective == 'pred_v':
            loss_weight = snr / (snr + 1)

        register_buffer('loss_weight', loss_weight)

        # whether to autonormalize
        self.normalize = normalize_to_neg_one_to_one if auto_normalize else identity
        self.unnormalize = unnormalize_to_zero_to_one if auto_normalize else identity
        self.clip_min = clip_min
        self.clip_max = clip_max


    def predict_start_from_noise(self, x_t, t, noise):
        return (
            extract(self.sqrt_recip_alphas_cumprod, t, x_t.shape) * x_t -
            extract(self.sqrt_recipm1_alphas_cumprod, t, x_t.shape) * noise
        )

    def predict_noise_from_start(self, x_t, t, x0):
        return (
            (extract(self.sqrt_recip_alphas_cumprod, t, x_t.shape) * x_t - x0) / \
            extract(self.sqrt_recipm1_alphas_cumprod, t, x_t.shape)
        )

    def predict_v(self, x_start, t, noise):
        return (
            extract(self.sqrt_alphas_cumprod, t, x_start.shape) * noise -
            extract(self.sqrt_one_minus_alphas_cumprod, t, x_start.shape) * x_start
        )

    def predict_start_from_v(self, x_t, t, v):
        return (
            extract(self.sqrt_alphas_cumprod, t, x_t.shape) * x_t -
            extract(self.sqrt_one_minus_alphas_cumprod, t, x_t.shape) * v
        )

    def q_posterior(self, x_start, x_t, t):
        posterior_mean = (
            extract(self.posterior_mean_coef1, t, x_t.shape) * x_start +
            extract(self.posterior_mean_coef2, t, x_t.shape) * x_t
        )
        posterior_variance = extract(self.posterior_variance, t, x_t.shape)
        posterior_log_variance_clipped = extract(self.posterior_log_variance_clipped, t, x_t.shape)
        return posterior_mean, posterior_variance, posterior_log_variance_clipped

    def model_predictions(self, x, t, cond, inits, cond_scale=1., rescaled_phi=0.0, clip_x_start=False, rederive_pred_noise = False):
        model_output = self.model.forward_with_cond_scale(x, t, cond, inits, cond_scale=cond_scale, rescaled_phi=rescaled_phi)
        
        if self.classifier_free_guidance:
            model_output_unguided = self.model.forward_with_cond_scale(x, t, torch.zeros_like(cond), torch.zeros_like(inits),
                                                                        cond_scale=cond_scale, rescaled_phi=rescaled_phi)
            model_output = model_output_unguided + self.guidance_scale * (model_output - model_output_unguided)
        
        
        maybe_clip = partial(torch.clamp, min = self.clip_min, max = self.clip_max) if clip_x_start else identity

        if self.objective == 'pred_noise':
            pred_noise = model_output
            x_start = self.predict_start_from_noise(x, t, pred_noise)
            x_start = maybe_clip(x_start)

            if clip_x_start and rederive_pred_noise:
                pred_noise = self.predict_noise_from_start(x, t, x_start)

        elif self.objective == 'pred_x0':
            x_start = model_output
            x_start = maybe_clip(x_start)
            pred_noise = self.predict_noise_from_start(x, t, x_start)

        elif self.objective == 'pred_v':
            v = model_output
            x_start = self.predict_start_from_v(x, t, v)
            x_start = maybe_clip(x_start)
            pred_noise = self.predict_noise_from_start(x, t, x_start)

        return ModelPrediction(pred_noise, x_start)

    def p_mean_variance(self, x, t, cond, inits, cond_scale, rescaled_phi, clip_denoised=True):
        preds = self.model_predictions(x, t, cond, inits, cond_scale, rescaled_phi,)
        x_start = preds.pred_x_start

        if clip_denoised:
            x_start.clamp_(self.clip_min, self.clip_max)

        model_mean, posterior_variance, posterior_log_variance = self.q_posterior(x_start = x_start, x_t = x, t = t)
        return model_mean, posterior_variance, posterior_log_variance, x_start

    def p_sample(self, x, t: int, cond, inits, cond_scale=1., rescaled_phi=0.0, clip_denoised=True):
        b, *_, device = *x.shape, x.device
        batched_times = torch.full((b,), t, device = x.device, dtype = torch.long)
        model_mean, _, model_log_variance, x_start = self.p_mean_variance(x=x, t=batched_times, cond=cond, inits=inits, cond_scale=cond_scale, rescaled_phi=rescaled_phi, clip_denoised=clip_denoised)
        noise = torch.randn_like(x) if t > 0 else 0. # no noise if t == 0
        pred_img = model_mean + (0.5 * model_log_variance).exp() * noise
        return pred_img, x_start

    def p_sample_loop(self, shape, cond, inits, cond_scale=1., rescaled_phi=0.0):
        batch, device = shape[0], self.betas.device

        img = torch.randn(shape, device=device)

        for t in tqdm(reversed(range(0, self.num_timesteps)), desc='sampling loop time step', total=self.num_timesteps):
            img, x_start = self.p_sample(img, t, cond, inits, cond_scale, rescaled_phi)

        img = self.unnormalize(img)
        return img

    def ddim_sample(self, shape, cond, inits, cond_scale=1., rescaled_phi=0.0, clip_denoised=True, starting_data = None, starting_timestep=None):
        batch, device, total_timesteps, sampling_timesteps, eta, objective = shape[0], self.betas.device, self.num_timesteps, self.sampling_timesteps, self.ddim_sampling_eta, self.objective

        if starting_timestep != None:
            total_timesteps = starting_timestep
            sampling_timesteps = starting_timestep

        times = torch.linspace(-1, total_timesteps - 1, steps=sampling_timesteps + 1)   # [-1, 0, 1, 2, ..., T-1] when sampling_timesteps == total_timesteps
        times = list(reversed(times.int().tolist()))
        time_pairs = list(zip(times[:-1], times[1:])) # [(T-1, T-2), (T-2, T-3), ..., (1, 0), (0, -1)]

        if starting_data == None:
            img = torch.randn(shape, device=device)
        else:
            img = starting_data.to(device)

        x_start = None

        for time, time_next in tqdm(time_pairs, desc='sampling loop time step'):
            time_cond = torch.full((batch,), time, device=device, dtype=torch.long)

            pred_noise, x_start, *_ = self.model_predictions(img, time_cond, cond, inits, cond_scale, rescaled_phi, clip_x_start = clip_denoised)

            if time_next < 0:
                img = x_start
                continue

            alpha = self.alphas_cumprod[time]
            alpha_next = self.alphas_cumprod[time_next]

            sigma = eta * ((1 - alpha / alpha_next) * (1 - alpha_next) / (1 - alpha)).sqrt()
            c = (1 - alpha_next - sigma ** 2).sqrt()

            noise = torch.randn_like(img)

            img = x_start * alpha_next.sqrt() + \
                  c * pred_noise + \
                  sigma * noise

        img = self.unnormalize(img)
        return img

    def sample(self, cond, no_grad=False, inits=None, cond_scale=1., rescaled_phi=0.0):
        if no_grad:
            with torch.no_grad():
                print("Gradients turned off")
                batch_size = cond.shape[0]
                seq_length, channels = self.seq_length, self.channels
                sample_fn = self.p_sample_loop if not self.is_ddim_sampling else self.ddim_sample
                samples = sample_fn((batch_size, channels, seq_length), cond, inits, cond_scale, rescaled_phi)

                return samples

        batch_size = cond.shape[0]
        seq_length, channels = self.seq_length, self.channels
        sample_fn = self.p_sample_loop if not self.is_ddim_sampling else self.ddim_sample
        samples = sample_fn((batch_size, channels, seq_length), cond, inits, cond_scale, rescaled_phi)

        return samples
    

    def inference(self, cond, inits, starting_timestep, starting_data, cond_scale=1., rescaled_phi=0.0,
                  no_grad=False):
        """
        Performs customized inference with condition "cond", starting at
        timestep "starting_timestep" with x_{starting_timestep} = starting_data
        """

        batch, device = cond.shape[0], self.betas.device
        
        if no_grad:
            with torch.no_grad():
                if starting_timestep < self.betas.shape[0]:
                    return self.ddim_sample(starting_data=starting_data.to(device), starting_timestep=starting_timestep,
                                            shape=(batch, self.channels, self.seq_length),
                                            cond=cond, inits=inits, cond_scale=cond_scale, rescaled_phi=rescaled_phi)

                img = starting_data.to(device)

                for t in tqdm(reversed(range(0, starting_timestep)), desc='sampling loop time step', total=starting_timestep):
                    img = self.p_sample(img, t, cond, inits, cond_scale, rescaled_phi)[0]

                img = self.unnormalize(img)
                return img
        
        if starting_timestep < self.betas.shape[0]:
            return self.ddim_sample(starting_data=starting_data.to(device), starting_timestep=starting_timestep,
                                    shape=(batch, self.channels, self.seq_length),
                                    cond=cond, inits=inits, cond_scale=cond_scale, rescaled_phi=rescaled_phi)

        img = starting_data.to(device)

        for t in tqdm(reversed(range(0, starting_timestep)), desc='sampling loop time step', total=starting_timestep):
            img = self.p_sample(img, t, cond, inits, cond_scale, rescaled_phi)[0]

        img = self.unnormalize(img)
        return img
    

    def diffuse(self, data, target_timestep, no_grad=False):
        """
        Diffuse x0 = data to x_{target_timestep}.
        """
        if no_grad:
            with torch.no_grad():
                noise = torch.randn_like(data)
                betas = self.betas
                alphas = 1. -  betas
                alpha_t = alphas[target_timestep - 1]
                return torch.sqrt(alpha_t) * data + torch.sqrt(1 - alpha_t) * noise
        
        noise = torch.randn_like(data)
        betas = self.betas
        alphas = 1. -  betas
        alpha_t = alphas[target_timestep - 1]
        return torch.sqrt(alpha_t) * data + torch.sqrt(1 - alpha_t) * noise


    def forward(self, conditions, initial_states):
        return self.sample(cond=conditions, inits=initial_states, no_grad=False)